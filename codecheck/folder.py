"""检查整个文件夹：找出所有 Python / JavaScript 文件，逐个做静态检查（不运行）。"""
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from collections import Counter

from pathlib import Path

from . import __version__, static_check, store
from .languages import SUFFIX

APP_ROOT = Path(__file__).resolve().parent.parent
from .proc import NO_WINDOW

EXT = SUFFIX  # 文件后缀 → 语言（定义在 languages.py）
IGNORE_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".env",
               ".idea", ".vscode", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
               "site-packages", ".tox", ".next", ".nuxt", "coverage", ".cache", "bower_components",
               "vendor", "third_party", "third-party", "target", "obj",
               # 浏览器 / Electron 的缓存目录
               "Cache", "Code Cache", "GPUCache", "Service Worker", "Crashpad", "ShaderCache",
               "GrShaderCache", "DawnCache", "blob_storage", "IndexedDB", "Local Storage"}
MAX_FILES = 500
MAX_SIZE = 1_000_000
GENERATED_SUFFIXES = (".min.js", ".bundle.js", "-bundle.js", ".prod.js", "_prod.js", ".min.mjs")


def _is_browser_profile(dirnames: list[str], filenames: list[str]) -> bool:
    """Chrome / Edge / Electron 的用户数据目录：里面是浏览器自己的文件和插件，不是你写的代码。"""
    files = set(filenames)
    return ("Local State" in files
            or ("Preferences" in files and ({"Extensions", "Cache", "Code Cache"} & set(dirnames)))
            or ("Secure Preferences" in files))


def _is_generated(path: str, name: str) -> bool:
    """压缩或打包生成的 JS（例如 jquery.min.js，或一行几千个字符的文件）。"""
    low = name.lower()
    if low.endswith(GENERATED_SUFFIXES):
        return True
    if not low.endswith((".js", ".mjs", ".cjs")):
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    return any(len(line) > 1000 for line in head.split(b"\n")[:20])


def _git_visible(folder: str) -> set[str] | None:
    """如果是 Git 项目，返回没有被 .gitignore 忽略的文件（相对路径）；不是 Git 项目返回 None。"""
    if not shutil.which("git") or not os.path.exists(os.path.join(folder, ".git")):
        return None
    try:
        r = subprocess.run(["git", "-C", folder, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
        if r.returncode != 0:
            return None
        return {os.path.normcase(os.path.normpath(p.decode("utf-8", "replace")))
                for p in r.stdout.split(b"\0") if p}
    except (OSError, subprocess.TimeoutExpired):
        return None


def _norm_rel(rel: str) -> str:
    return os.path.normcase(os.path.normpath(rel.replace("/", os.sep).replace("\\", os.sep)))


def find_files(folder: str, exclude: list[str] | None = None) -> tuple[list[str], bool, dict]:
    """返回 (文件列表, 是否因为太多被截断, 跳过统计)。exclude 是要排除的子文件夹（相对路径）。"""
    excl = {_norm_rel(e) for e in (exclude or []) if e.strip()}
    git = _git_visible(folder)
    skipped = {"browser_profiles": [], "generated": 0, "gitignored": 0, "excluded": 0}
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        rel_dir = os.path.relpath(dirpath, folder)
        if rel_dir != "." and _norm_rel(rel_dir) in excl:
            skipped["excluded"] += 1
            dirnames[:] = []
            continue
        if _is_browser_profile(dirnames, filenames):
            skipped["browser_profiles"].append(rel_dir.replace("\\", "/"))
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in EXT:
                continue
            path = os.path.join(dirpath, name)
            if git is not None and _norm_rel(os.path.relpath(path, folder)) not in git:
                skipped["gitignored"] += 1
                continue
            if _is_generated(path, name):
                skipped["generated"] += 1
                continue
            try:
                if os.path.getsize(path) > MAX_SIZE:
                    skipped["generated"] += 1
                    continue
            except OSError:
                continue
            found.append(path)
            if len(found) >= MAX_FILES:
                return found, True, skipped
    return found, False, skipped


def dir_summary(files: list[dict], depth: int = 2, limit: int = 24) -> list[dict]:
    """按子文件夹统计文件数（最多两层），方便用户选择排除哪些文件夹。"""
    counts: dict[str, int] = {}
    for f in files:
        parts = f["rel"].replace("\\", "/").split("/")[:-1]
        for d in range(1, min(depth, len(parts)) + 1):
            key = "/".join(parts[:d])
            counts[key] = counts.get(key, 0) + 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"dir": k, "files": v} for k, v in sorted(items, key=lambda kv: kv[0].lower())]


def group_by_dir(rels: list[str], depth: int = 2) -> list[dict]:
    """把一长串文件按文件夹分组：[{dir, files}]。"""
    counts: dict[str, int] = {}
    for rel in rels:
        parts = rel.replace("\\", "/").split("/")[:-1]
        key = "/".join(parts[:depth]) or "（根目录）"
        counts[key] = counts.get(key, 0) + 1
    return [{"dir": k, "files": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def _line_count(path: str) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def scan(folder: str, exclude: list[str] | None = None, compare: bool = True) -> dict:
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError("文件夹不存在")
    if exclude is None:
        exclude = store.get("exclude", folder, [])
    files, truncated, skipped = find_files(folder, exclude)
    issues: dict[str, list] = {f: [] for f in files}
    notes = []

    py = [f for f in files if EXT[os.path.splitext(f)[1].lower()] == "python"]
    js = [f for f in files if EXT[os.path.splitext(f)[1].lower()] == "javascript"]
    if py:
        try:
            for path, found in static_check.ruff_many(py).items():
                issues.setdefault(path, []).extend(found)
        except RuntimeError as e:
            notes.append(str(e))
    if js:
        if shutil.which("node"):
            with ThreadPoolExecutor(max_workers=8) as pool:
                for path, found in zip(js, pool.map(static_check.node_syntax, js), strict=True):
                    issues[path].extend(found)
        else:
            notes.append("没有安装 Node.js，JavaScript 文件只列出、没有检查")

    out = []
    for path in files:
        found = sorted(issues.get(path, []), key=lambda i: (i.severity != "error", i.line))
        out.append({
            "path": path,
            "rel": os.path.relpath(path, folder),
            "language": EXT[os.path.splitext(path)[1].lower()],
            "lines": _line_count(path),
            "errors": sum(1 for i in found if i.severity == "error"),
            "warnings": sum(1 for i in found if i.severity != "error"),
            "issues": [i.to_dict() for i in found],
        })
    out.sort(key=lambda f: (-f["errors"], -f["warnings"], f["rel"].lower()))
    result = {
        "folder": folder,
        "files": out,
        "total": len(out),
        "with_errors": sum(1 for f in out if f["errors"]),
        "with_warnings": sum(1 for f in out if not f["errors"] and f["warnings"]),
        "truncated": truncated,
        "notes": notes,
        "skipped": skipped,
        "exclude": list(exclude or []),
        "dirs": dir_summary(out),
    }
    if compare:
        result["compare"] = compare_with_last(result)
    result["copy_warning"] = copy_warning(folder)
    return result


def copy_warning(folder: str) -> dict | None:
    """选的文件夹是不是“代码检查助手”的另一个副本？版本不一样时提醒：它不是正在运行的程序。"""
    f = Path(folder)
    if not ((f / "codecheck" / "__init__.py").is_file() and (f / "version.txt").is_file()):
        return None
    if f.resolve() == APP_ROOT:
        return None
    try:
        ver = (f / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        ver = "未知"
    if ver == __version__:
        return None
    return {"version": ver, "running": __version__, "running_path": str(APP_ROOT)}


def _keys(files: list[dict]) -> list[tuple]:
    # 用“文件 + 规则 + 说明”判断是不是同一个问题；不用行号，因为改代码后行号会变
    return [(f["rel"].replace("\\", "/"), i["code"], i["message"], i["line"], i["severity"])
            for f in files for i in f["issues"]]


def compare_with_last(r: dict) -> dict | None:
    """和上一次检查这个文件夹的结果比较，并把这次的结果存起来。"""
    now_items = _keys(r["files"])
    now_files = sorted(f["rel"].replace("\\", "/") for f in r["files"])
    last = store.get("scan", r["folder"])
    store.put("scan", r["folder"], {"time": store.now(), "items": now_items, "files": now_files})
    if not last:
        return None
    # 只比较两次都检查过的文件：
    #   这次没检查的文件（新排除的、删掉的）里的问题，不能算“已解决”；
    #   上次没检查、这次才加入的文件里的问题，不能算“新出现”
    now_set = set(now_files)
    last_files = set(last["files"]) if last.get("files") is not None else None  # 旧记录没有文件列表
    left_out = {x[0] for x in last["items"]} - now_set
    newly_in = (now_set - last_files) if last_files is not None else set()
    old = Counter(tuple(x[:3]) for x in last["items"] if x[0] in now_set)
    new = Counter(tuple(x[:3]) for x in now_items if last_files is None or x[0] in last_files)
    gone, added = old - new, new - old
    line_old = {tuple(x[:3]): x for x in last["items"]}
    line_new = {tuple(x[:3]): x for x in now_items}

    def lst(counter, lookup):
        out = []
        for k, n in counter.items():
            x = lookup[k]
            out += [{"rel": x[0], "code": x[1], "message": x[2], "line": x[3], "severity": x[4]}] * n
        return sorted(out, key=lambda i: (i["severity"] != "error", i["rel"]))[:100]

    return {"last_time": last["time"], "last_total": len(last["items"]), "now_total": len(now_items),
            "left_out_files": len(left_out), "newly_in_files": len(newly_in),
            "resolved": lst(gone, line_old), "new": lst(added, line_new),
            "resolved_count": sum(gone.values()), "new_count": sum(added.values()),
            "remaining_count": len(now_items) - sum(added.values())}


def format_report(r: dict) -> str:
    lines = [f"文件夹：{r['folder']}",
             f"共 {r['total']} 个代码文件，{r['with_errors']} 个有错误，{r['with_warnings']} 个只有警告"]
    c = r.get("compare")
    if c:
        lines.append(f"和上次（{c['last_time']}）相比：已解决 {c['resolved_count']} 个，"
                     f"还剩 {c['remaining_count']} 个，新出现 {c['new_count']} 个")
        for i in c["resolved"]:
            lines.append(f"    ✓ 已解决 {i['rel']} [{i['code']}] {i['message']}")
        for i in c["new"]:
            lines.append(f"    + 新问题 {i['rel']} 第 {i['line']} 行 [{i['code']}] {i['message']}")
    if r["truncated"]:
        lines.append(f"（文件太多，只检查了前 {MAX_FILES} 个）")
    sk = r.get("skipped") or {}
    auto = []
    if sk.get("browser_profiles"):
        auto.append(f"浏览器配置文件夹 {len(sk['browser_profiles'])} 个")
    if sk.get("generated"):
        auto.append(f"压缩/生成的文件 {sk['generated']} 个")
    if sk.get("gitignored"):
        auto.append(f".gitignore 忽略的文件 {sk['gitignored']} 个")
    if auto:
        lines.append("已自动跳过：" + "、".join(auto))
    lines += r["notes"]
    for f in r["files"]:
        if not f["issues"]:
            continue
        lines.append(f"\n{'✗' if f['errors'] else '!'} {f['rel']}（{f['errors']} 个错误，{f['warnings']} 个警告）")
        for i in f["issues"]:
            tag = "错误" if i["severity"] == "error" else "警告"
            lines.append(f"    {tag} 第 {i['line']} 行 [{i['code']}] {i['message']}")
    ok = sum(1 for f in r["files"] if not f["issues"])
    if ok:
        lines.append(f"\n✓ 另外 {ok} 个文件没有发现问题")
    return "\n".join(lines)
