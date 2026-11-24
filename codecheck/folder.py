"""检查整个文件夹：找出所有 Python / JavaScript 文件，逐个做静态检查（不运行）。"""
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

from . import static_check
from .proc import NO_WINDOW

EXT = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript"}
IGNORE_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".env",
               ".idea", ".vscode", "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
               "site-packages", ".tox", ".next", ".nuxt", "coverage", ".cache", "bower_components"}
MAX_FILES = 500
MAX_SIZE = 1_000_000


def find_files(folder: str) -> tuple[list[str], bool]:
    """返回 (文件列表, 是否因为太多被截断)。"""
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in EXT:
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(path) <= MAX_SIZE:
                        found.append(path)
                except OSError:
                    continue
                if len(found) >= MAX_FILES:
                    return found, True
    return found, False


def _node_check(path: str) -> list[static_check.Issue]:
    p = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=20,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if p.returncode == 0:
        return []
    line = 0
    first = p.stderr.splitlines()[0] if p.stderr else ""
    if ":" in first:
        try:
            line = int(first.rsplit(":", 1)[1])
        except ValueError:
            pass
    msg = next((ln for ln in p.stderr.splitlines() if "Error" in ln), p.stderr.strip()[:200])
    msg = msg.strip().removeprefix("SyntaxError:").strip()
    return [static_check.Issue(line, 0, "SyntaxError", "语法错误：" + msg, "error")]


def _line_count(path: str) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def scan(folder: str) -> dict:
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise ValueError("文件夹不存在")
    files, truncated = find_files(folder)
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
                for path, found in zip(js, pool.map(_node_check, js), strict=True):
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
    return {
        "folder": folder,
        "files": out,
        "total": len(out),
        "with_errors": sum(1 for f in out if f["errors"]),
        "with_warnings": sum(1 for f in out if not f["errors"] and f["warnings"]),
        "truncated": truncated,
        "notes": notes,
    }


def format_report(r: dict) -> str:
    lines = [f"文件夹：{r['folder']}",
             f"共 {r['total']} 个代码文件，{r['with_errors']} 个有错误，{r['with_warnings']} 个只有警告"]
    if r["truncated"]:
        lines.append(f"（文件太多，只检查了前 {MAX_FILES} 个）")
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
