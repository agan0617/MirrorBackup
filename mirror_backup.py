"""增減量（鏡像）備份：讓每一組 dest 資料夾與對應的 src 資料夾完全相同。

流程：
  1. 逐組掃描 src 與 dest 的所有檔案與資料夾
  2. 把每一組的差異（新增／更新／刪除／更名）印在 console 上
  3. 使用者輸入 y 同意後才動手，讓每一組的 dest 與 src 完全相同
  4. 重新比對驗證，產出執行結果報告（所有組別寫在同一份）

用法：
  python mirror_backup.py                     # 讀同目錄的 backup.ini
  python mirror_backup.py --config 其他.ini

判定規則：
  - 檔案以「大小＋修改時間」判斷是否相同（修改時間容許 2 秒誤差，相容 FAT/exFAT 隨身碟）
  - 路徑比對不分大小寫（Windows 檔案系統行為）；只有大小寫不同時改名成 src 的寫法
  - 符號連結與 junction 兩邊都略過、不跟進，避免順著連結刪到別處
  - src 有讀不到的資料夾或檔案時，dest 對應位置的東西一律保留不刪
"""

import argparse
import configparser
import os
import shutil
import stat
import sys
import time
import unicodedata
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(SCRIPT_DIR, "backup.ini")
SETTINGS_SECTION = "settings"
MTIME_TOLERANCE_NS = 2_000_000_000
TMP_SUFFIX = ".mbtmp"


# ---------------------------------------------------------------- 小工具

def lp(path):
    """加上 \\\\?\\ 前綴，讓超過 260 字元的長路徑也能操作。"""
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def key_of(rel):
    return os.path.normcase(rel)


def depth(rel):
    return rel.count(os.sep)


def contains(outer, inner):
    """outer 是否等於 inner 或包含 inner（不分大小寫）。"""
    a = key_of(outer).rstrip(os.sep) + os.sep
    b = key_of(inner).rstrip(os.sep) + os.sep
    return b.startswith(a)


def overlaps(a, b):
    return contains(a, b) or contains(b, a)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def pad(text, width):
    """依顯示寬度補空白（中文字佔兩格）。"""
    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - shown)


def clear_readonly(path):
    try:
        mode = os.stat(path).st_mode
        if not mode & stat.S_IWRITE:
            os.chmod(path, mode | stat.S_IWRITE)
    except OSError:
        pass


def is_link(entry):
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)
    return bool(is_junction and is_junction())


class Color:
    enabled = sys.stdout.isatty()
    if enabled and os.name == "nt":
        os.system("")  # 開啟 Windows console 的 ANSI 色碼支援

    @classmethod
    def wrap(cls, code, text):
        return f"\033[{code}m{text}\033[0m" if cls.enabled else text


def green(t): return Color.wrap("32", t)
def yellow(t): return Color.wrap("33", t)
def red(t): return Color.wrap("31", t)
def cyan(t): return Color.wrap("36", t)
def bold(t): return Color.wrap("1", t)


# ---------------------------------------------------------------- 設定檔

class ConfigError(Exception):
    pass


class Job:
    """一組要備份的目錄。"""

    def __init__(self, name, src, dest):
        self.name = name
        self.src = src
        self.dest = dest
        self.error = ""        # 執行前就發現的問題（例如 src 不存在），有就整組略過
        self.src_tree = None
        self.plan = None
        self.log = None
        self.verify_plan = None
        self.result = ""


class Config:
    """[settings] 放共用設定，其餘每個區段是一組備份目錄，區段名稱就是組名。"""

    def __init__(self, path):
        if not os.path.isfile(path):
            raise ConfigError(f"找不到設定檔：{path}")
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8-sig")
        except configparser.Error as e:
            raise ConfigError(f"設定檔格式錯誤：{e}")
        self.path = os.path.abspath(path)

        report_dir = "reports"
        self.jobs = []
        for section in parser.sections():
            sec = parser[section]
            if section.lower() == SETTINGS_SECTION:
                self._check_keys(section, sec, {"report_dir"})
                report_dir = sec.get("report_dir", "").strip() or report_dir
                continue
            self._check_keys(section, sec, {"src", "dest"})
            self.jobs.append(Job(section, self._dir(sec, "src", section), self._dir(sec, "dest", section)))
        if not self.jobs:
            raise ConfigError("設定檔裡沒有任何一組備份目錄（每組寫成一個 [名稱] 區段，含 src 與 dest）")

        if not os.path.isabs(report_dir):
            report_dir = os.path.join(SCRIPT_DIR, report_dir)
        self.report_dir = os.path.normpath(report_dir)
        self._validate()

    @staticmethod
    def _check_keys(section, sec, allowed):
        unknown = sorted(set(sec.keys()) - allowed)
        if unknown:
            raise ConfigError(f"[{section}] 有不認得的欄位：{'、'.join(unknown)}"
                              f"（可用的欄位：{'、'.join(sorted(allowed))}）")

    @staticmethod
    def _dir(sec, name, section):
        value = sec.get(name, "").strip().strip('"')
        if not value:
            raise ConfigError(f"[{section}] 沒有填 {name}")
        return os.path.normpath(os.path.abspath(value))

    def _validate(self):
        # 會讓組與組互相干擾的設定，整份拒絕執行
        for i, a in enumerate(self.jobs):
            if overlaps(a.src, a.dest):
                raise ConfigError(f"[{a.name}] 的 src 與 dest 相同或互相包含")
            if contains(a.dest, self.report_dir):
                raise ConfigError(f"report_dir 不能放在 [{a.name}] 的 dest 裡面（報告會變成多出來的檔案）")
            for b in self.jobs[i + 1:]:
                if overlaps(a.dest, b.dest):
                    raise ConfigError(f"[{a.name}] 與 [{b.name}] 的 dest 相同或互相包含，兩組會互刪對方的檔案")
            for b in self.jobs:
                if b is not a and overlaps(a.dest, b.src):
                    raise ConfigError(f"[{a.name}] 的 dest 與 [{b.name}] 的 src 相同或互相包含，"
                                      f"備份結果會隨執行順序而不同")

        # 只影響單一組的問題（例如隨身碟沒插），那一組略過、其他組照常
        for job in self.jobs:
            if not os.path.isdir(job.src):
                job.error = f"src 資料夾不存在：{job.src}"
            elif os.path.exists(job.dest) and not os.path.isdir(job.dest):
                job.error = f"dest 不是資料夾：{job.dest}"


# ---------------------------------------------------------------- 掃描

class Tree:
    """一個資料夾底下的所有檔案與子資料夾，key 為不分大小寫的相對路徑。"""

    def __init__(self, root):
        self.root = root
        self.files = {}      # key -> (rel, size, mtime_ns)
        self.dirs = {}       # key -> rel
        self.skipped = []    # 略過的連結（rel）
        self.errors = []     # (rel, 錯誤訊息)
        self.unknown = []    # 內容不明的 key（讀不到或略過），dest 對應的東西不可刪

    @property
    def total_size(self):
        return sum(f[1] for f in self.files.values())


def scan(root):
    tree = Tree(root)
    if not os.path.isdir(root):
        return tree
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        full_dir = os.path.join(root, rel_dir) if rel_dir else root
        try:
            with os.scandir(lp(full_dir)) as it:
                entries = list(it)
        except OSError as e:
            tree.errors.append((rel_dir or ".", e.strerror or str(e)))
            tree.unknown.append(key_of(rel_dir))
            continue
        for entry in entries:
            rel = os.path.join(rel_dir, entry.name) if rel_dir else entry.name
            try:
                if is_link(entry):
                    tree.skipped.append(rel)
                    tree.unknown.append(key_of(rel))
                elif entry.is_dir(follow_symlinks=False):
                    tree.dirs[key_of(rel)] = rel
                    stack.append(rel)
                else:
                    st = entry.stat(follow_symlinks=False)
                    tree.files[key_of(rel)] = (rel, st.st_size, st.st_mtime_ns)
            except OSError as e:
                tree.errors.append((rel, e.strerror or str(e)))
                tree.unknown.append(key_of(rel))
    return tree


# ---------------------------------------------------------------- 比對

class Plan:
    def __init__(self):
        self.del_files = []   # (rel, size)
        self.del_dirs = []    # rel
        self.rename = []      # (dest 原本的 rel, src 的 rel)
        self.mkdir = []       # rel
        self.new = []         # (rel, size)
        self.update = []      # (rel, size, 原因)
        self.protected = []   # dest 有、src 讀不到，所以保留不刪的 rel

    @property
    def change_count(self):
        return (len(self.del_files) + len(self.del_dirs) + len(self.rename)
                + len(self.mkdir) + len(self.new) + len(self.update))

    @property
    def copy_bytes(self):
        return sum(x[1] for x in self.new) + sum(x[1] for x in self.update)


def _under(key, prefixes):
    return any(key == p or key.startswith(p + os.sep) for p in prefixes if p)


def diff(src, dest):
    plan = Plan()

    for k, rel in src.dirs.items():
        if k in dest.dirs:
            if dest.dirs[k] != rel:
                plan.rename.append((dest.dirs[k], rel))
        else:
            plan.mkdir.append(rel)

    for k, (rel, size, mtime) in src.files.items():
        d = dest.files.get(k)
        if d is None:
            plan.new.append((rel, size))
            continue
        d_rel, d_size, d_mtime = d
        if d_rel != rel:
            plan.rename.append((d_rel, rel))
        reasons = []
        if d_size != size:
            reasons.append(f"大小 {human(d_size)} → {human(size)}")
        if abs(d_mtime - mtime) > MTIME_TOLERANCE_NS:
            reasons.append("修改時間不同" + ("（dest 較新）" if d_mtime > mtime else ""))
        if reasons:
            plan.update.append((rel, size, "、".join(reasons)))

    for k, (rel, size, _) in dest.files.items():
        if k in src.files:
            continue
        if _under(k, src.unknown):
            plan.protected.append(rel)
        else:
            plan.del_files.append((rel, size))

    for k, rel in dest.dirs.items():
        if k in src.dirs:
            continue
        if _under(k, src.unknown):
            plan.protected.append(rel)
        else:
            plan.del_dirs.append(rel)

    plan.del_files.sort()
    plan.del_dirs.sort(key=lambda r: (-depth(r), r))     # 由深到淺刪
    plan.rename.sort(key=lambda x: (depth(x[1]), x[1]))  # 由淺到深改名
    plan.mkdir.sort(key=lambda r: (depth(r), r))
    plan.new.sort()
    plan.update.sort()
    plan.protected.sort()
    return plan


def plan_lines(plan):
    """差異清單的文字（console 與報告共用），回傳 [(色彩函式, 文字)]。"""
    lines = []
    for rel, size in plan.new:
        lines.append((green, f"  [+] 新增      {rel}  ({human(size)})"))
    for rel, size, why in plan.update:
        lines.append((yellow, f"  [*] 更新      {rel}  ({why})"))
    for old, new in plan.rename:
        lines.append((cyan, f"  [~] 改大小寫  {old} → {new}"))
    for rel in plan.mkdir:
        lines.append((green, f"  [+] 新增資料夾 {rel}{os.sep}"))
    for rel, size in plan.del_files:
        lines.append((red, f"  [-] 刪除      {rel}  ({human(size)})"))
    for rel in plan.del_dirs:
        lines.append((red, f"  [-] 刪除資料夾 {rel}{os.sep}"))
    return lines


def plan_summary(plan):
    return (f"新增 {len(plan.new)} 檔、更新 {len(plan.update)} 檔、刪除 {len(plan.del_files)} 檔、"
            f"改大小寫 {len(plan.rename)} 項、新增資料夾 {len(plan.mkdir)} 個、"
            f"刪除資料夾 {len(plan.del_dirs)} 個；需複製 {human(plan.copy_bytes)}")


# ---------------------------------------------------------------- 執行

ACTIONS = ["刪除檔案", "刪除資料夾", "改大小寫", "新增資料夾", "新增檔案", "更新檔案"]

RESULT_SAME = "相同，不需備份"
RESULT_OK = "成功"
RESULT_NOT_RUN = "未執行（前面的組別被中斷）"


class Log:
    def __init__(self):
        self.items = []  # (動作, rel, 成功?, 錯誤訊息)
        self.copied_bytes = 0
        self.interrupted = False

    def add(self, action, rel, ok, err=""):
        self.items.append((action, rel, ok, err))

    def count(self, action, ok):
        return sum(1 for a, _, o, _ in self.items if a == action and o == ok)

    @property
    def failures(self):
        return [x for x in self.items if not x[2]]


def copy_file(src_full, dst_full):
    """先複製到暫存檔再替換，中途失敗不會留下半個檔案蓋掉舊版。"""
    tmp = dst_full + TMP_SUFFIX
    os.makedirs(os.path.dirname(dst_full), exist_ok=True)
    try:
        shutil.copy2(src_full, tmp)
        if os.path.exists(dst_full):
            clear_readonly(dst_full)
        os.replace(tmp, dst_full)
    except BaseException:
        try:
            if os.path.exists(tmp):
                clear_readonly(tmp)
                os.remove(tmp)
        except OSError:
            pass
        raise


def execute(job, log):
    plan = job.plan
    total = plan.change_count
    done = 0

    def step(action, rel, fn):
        nonlocal done
        done += 1
        try:
            fn()
            log.add(action, rel, True)
            print(f"  [{done}/{total}] {action} {rel}")
        except OSError as e:
            msg = e.strerror or str(e)
            log.add(action, rel, False, msg)
            print(red(f"  [{done}/{total}] {action} 失敗 {rel}：{msg}"))

    def dpath(rel):
        return lp(os.path.join(job.dest, rel))

    def spath(rel):
        return lp(os.path.join(job.src, rel))

    os.makedirs(lp(job.dest), exist_ok=True)

    for rel, _ in plan.del_files:
        p = dpath(rel)
        step("刪除檔案", rel, lambda p=p: (clear_readonly(p), os.remove(p)))

    for rel in plan.del_dirs:
        p = dpath(rel)
        step("刪除資料夾", rel, lambda p=p: (clear_readonly(p), os.rmdir(p)))

    for old, new in plan.rename:
        a, b = dpath(old), dpath(new)
        step("改大小寫", f"{old} → {new}", lambda a=a, b=b: os.rename(a, b))

    for rel in plan.mkdir:
        p = dpath(rel)
        step("新增資料夾", rel, lambda p=p: os.makedirs(p, exist_ok=True))

    for action, items in (("新增檔案", plan.new), ("更新檔案", plan.update)):
        for item in items:
            rel, size = item[0], item[1]
            s, d = spath(rel), dpath(rel)

            def do(s=s, d=d, size=size):
                copy_file(s, d)
                log.copied_bytes += size

            step(action, rel, do)


def job_result(job):
    if job.error:
        return f"略過：{job.error}"
    if job.log is None:
        return RESULT_SAME if job.plan.change_count == 0 else RESULT_NOT_RUN
    if job.log.interrupted:
        return "中斷（使用者按了 Ctrl+C）"
    if job.log.failures:
        return f"部分失敗（{len(job.log.failures)} 項）"
    if job.verify_plan.change_count:
        return "執行完成，但驗證時仍有差異"
    return RESULT_OK


# ---------------------------------------------------------------- 報告

def job_report(job):
    w = [f"■ [{job.name}]",
         f"  來源 src：{job.src}",
         f"  目的 dest：{job.dest}",
         f"  結果：{job.result}"]
    if job.error:
        return w
    plan, log, src = job.plan, job.log, job.src_tree

    if log is not None:
        w.append("")
        w.append("  [摘要]")
        w.append(f"    {pad('動作', 12)}{'預計':>6}{'成功':>6}{'失敗':>6}")
        planned = {
            "刪除檔案": len(plan.del_files), "刪除資料夾": len(plan.del_dirs),
            "改大小寫": len(plan.rename), "新增資料夾": len(plan.mkdir),
            "新增檔案": len(plan.new), "更新檔案": len(plan.update),
        }
        for a in ACTIONS:
            w.append(f"    {pad(a, 12)}{planned[a]:>8}{log.count(a, True):>8}{log.count(a, False):>8}")
        w.append(f"    複製資料量：{human(log.copied_bytes)}（預計 {human(plan.copy_bytes)}）")
    elif plan.change_count:
        w.append("")
        w.append(f"  [未執行的差異] {plan_summary(plan)}")
        w.extend("  " + t for _, t in plan_lines(plan))
    w.append(f"  src 共 {len(src.files)} 個檔案、{len(src.dirs)} 個資料夾，{human(src.total_size)}")

    if job.verify_plan is not None:
        w.append("")
        w.append("  [驗證：執行後重新比對 src 與 dest]")
        if job.verify_plan.change_count == 0:
            w.append("    完全相同")
        else:
            w.append(f"    仍有差異：{plan_summary(job.verify_plan)}")
            w.extend("  " + t for _, t in plan_lines(job.verify_plan))

    if log is not None and log.failures:
        w.append("")
        w.append(f"  [失敗項目] {len(log.failures)} 項")
        w.extend(f"    {a} {rel}：{err}" for a, rel, _, err in log.failures)
    if src.errors:
        w.append("")
        w.append(f"  [src 讀取錯誤] {len(src.errors)} 項（這些位置在 dest 的對應內容保留不動）")
        w.extend(f"    {rel}：{err}" for rel, err in src.errors)
    if plan.protected:
        w.append("")
        w.append(f"  [保留未刪] {len(plan.protected)} 項（src 對應位置讀不到或是連結）")
        w.extend(f"    {rel}" for rel in plan.protected)
    if src.skipped:
        w.append("")
        w.append(f"  [略過的連結] {len(src.skipped)} 項（符號連結／junction 不備份）")
        w.extend(f"    {rel}" for rel in src.skipped)

    if log is not None:
        w.append("")
        w.append(f"  [執行明細] {len(log.items)} 項")
        w.extend(f"    {'OK  ' if ok else 'FAIL'} {a} {rel}" + (f"：{err}" if err else "")
                 for a, rel, ok, err in log.items)
    return w


def write_report(cfg, overall, started, finished):
    os.makedirs(cfg.report_dir, exist_ok=True)
    path = os.path.join(cfg.report_dir, f"backup_report_{started:%Y%m%d_%H%M%S}.txt")
    copied = sum(j.log.copied_bytes for j in cfg.jobs if j.log)

    w = ["增減量備份執行報告", "=" * 40,
         f"開始時間：{started:%Y-%m-%d %H:%M:%S}",
         f"結束時間：{finished:%Y-%m-%d %H:%M:%S}（耗時 {(finished - started).total_seconds():.1f} 秒）",
         f"設定檔：  {cfg.path}",
         f"整體結果：{overall}",
         f"複製資料量：{human(copied)}",
         "",
         f"[各組結果] 共 {len(cfg.jobs)} 組"]
    width = max(len(j.name) for j in cfg.jobs) * 2 + 4
    w.extend(f"  {pad('[' + j.name + ']', width)}{j.result}" for j in cfg.jobs)
    for job in cfg.jobs:
        w.append("")
        w.append("-" * 40)
        w.extend(job_report(job))

    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(w) + "\n")
    return path


# ---------------------------------------------------------------- 主程式

def print_scan_problems(src, dest):
    for label, tree in (("src", src), ("dest", dest)):
        if tree.errors:
            print(red(f"  {label} 有 {len(tree.errors)} 個位置讀不到："))
            for rel, err in tree.errors[:20]:
                print(red(f"    {rel}：{err}"))
            if len(tree.errors) > 20:
                print(red(f"    …其餘 {len(tree.errors) - 20} 項見報告"))
        if tree.skipped:
            print(yellow(f"  {label} 有 {len(tree.skipped)} 個符號連結／junction，略過不處理"))


def prepare(job):
    """掃描一組目錄並印出差異。"""
    print(bold(f"■ [{job.name}]"))
    print(f"  來源 src ：{job.src}")
    dest_note = "" if job.error or os.path.isdir(job.dest) else yellow("（尚不存在，會自動建立）")
    print(f"  目的 dest：{job.dest}" + dest_note)
    if job.error:
        print(red(f"  略過：{job.error}"))
        print()
        return

    t0 = time.time()
    job.src_tree = scan(job.src)
    dest = scan(job.dest)
    src = job.src_tree
    print(f"  src  {len(src.files)} 個檔案、{len(src.dirs)} 個資料夾，{human(src.total_size)}")
    print(f"  dest {len(dest.files)} 個檔案、{len(dest.dirs)} 個資料夾，{human(dest.total_size)}"
          f"（掃描耗時 {time.time() - t0:.1f} 秒）")
    print_scan_problems(src, dest)

    plan = job.plan = diff(src, dest)
    if plan.protected:
        print(yellow(f"  有 {len(plan.protected)} 項 dest 內容因 src 對應位置讀不到而保留不刪（明細見報告）"))
    if plan.change_count == 0:
        print(green("  完全相同，不需要備份。"))
    else:
        print("  差異清單：")
        for color, text in plan_lines(plan):
            print(color("  " + text))
        print("  合計：" + plan_summary(plan))
        if plan.del_files and not src.files:
            print(red("  警告：src 裡沒有任何檔案，執行後 dest 會被清空！請確認 src 路徑是否正確。"))
    print()


def main():
    ap = argparse.ArgumentParser(description="增減量（鏡像）備份：讓每一組 dest 與 src 完全相同")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="設定檔路徑（預設為同目錄的 backup.ini）")
    args = ap.parse_args()

    try:
        cfg = Config(args.config)
    except ConfigError as e:
        print(red(f"設定錯誤：{e}"))
        return 2

    print(f"設定檔：{cfg.path}（共 {len(cfg.jobs)} 組）")
    print()
    for job in cfg.jobs:
        prepare(job)

    todo = [j for j in cfg.jobs if not j.error and j.plan.change_count]
    skipped = [j for j in cfg.jobs if j.error]

    print(bold("總覽："))
    width = max(len(j.name) for j in cfg.jobs) * 2 + 4
    for job in cfg.jobs:
        if job.error:
            line = red("略過（設定有問題）")
        elif job.plan.change_count == 0:
            line = green("相同")
        else:
            line = (f"新增 {len(job.plan.new)}、更新 {len(job.plan.update)}、"
                    f"刪除 {len(job.plan.del_files)} 檔，需複製 {human(job.plan.copy_bytes)}")
        print(f"  {pad('[' + job.name + ']', width)}{line}")
    print()

    if not todo:
        if skipped:
            print(red(f"沒有需要備份的組別，但有 {len(skipped)} 組因設定問題被略過。"))
            return 1
        print(green("所有組別的 src 與 dest 都完全相同，不需要備份。"))
        return 0

    deletes = sum(len(j.plan.del_files) for j in todo)
    prompt = f"確定要執行備份嗎？共 {len(todo)} 組，dest 中合計 {deletes} 個檔案會被刪除 (y/N)："
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        answer = ""
    # PowerShell 5.1 用管線餵輸入時會在前面加 BOM
    if answer.strip().lstrip("﻿").lower() not in ("y", "yes"):
        print("已取消，沒有做任何變更。")
        return 0

    started = datetime.now()
    for job in todo:
        print()
        print(bold(f"執行 [{job.name}]…"))
        job.log = Log()
        try:
            execute(job, job.log)
        except KeyboardInterrupt:
            job.log.interrupted = True
            print(red("\n已中斷，其餘組別不執行，產生目前為止的報告…"))
        print("  驗證中（重新比對 src 與 dest）…")
        job.src_tree = scan(job.src)
        job.verify_plan = diff(job.src_tree, scan(job.dest))
        if job.log.interrupted:
            break
    finished = datetime.now()

    for job in cfg.jobs:
        job.result = job_result(job)
    good = (RESULT_OK, RESULT_SAME)
    bad = [j for j in cfg.jobs if j.result not in good]
    overall = "全部成功" if not bad else f"{len(bad)} 組有問題"
    report = write_report(cfg, overall, started, finished)

    print()
    print(bold("執行結果："))
    for job in cfg.jobs:
        color = green if job.result in good else red
        print(f"  {pad('[' + job.name + ']', width)}{color(job.result)}")
    copied = sum(j.log.copied_bytes for j in cfg.jobs if j.log)
    print(f"複製 {human(copied)}，耗時 {(finished - started).total_seconds():.1f} 秒")
    print(f"報告：{report}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
