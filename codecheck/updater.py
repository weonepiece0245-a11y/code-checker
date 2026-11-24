"""启动时自动检查并下载新版本。

更新来源写在项目根目录的 update_source.txt 里，格式是 GitHub 的 "用户名/仓库名"。
文件为空就不检查更新。网络不通、下载失败都会直接跳过，不影响正常使用。

退出码：0 = 没有更新或已跳过；10 = 更新完成，需要重新启动启动器。
"""
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = "启动网页版.bat"
KEEP = {".deps_ok", "update_source.txt"}  # 本地文件，不被覆盖


def _ver(s: str) -> tuple:
    try:
        return tuple(int(x) for x in s.strip().split("."))
    except ValueError:
        return (0,)


def _get(url: str, timeout: int) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "codecheck-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def main() -> int:
    src_file = ROOT / "update_source.txt"
    source = src_file.read_text(encoding="utf-8").strip() if src_file.exists() else ""
    if not source:
        return 0
    local = (ROOT / "version.txt").read_text(encoding="utf-8").strip()
    try:
        remote = _get(f"https://raw.githubusercontent.com/{source}/main/version.txt", 5).decode().strip()
    except Exception:
        print("（检查更新失败，可能没联网，先用当前版本）")
        return 0
    if _ver(remote) <= _ver(local):
        print(f"当前已是最新版本 {local}")
        return 0

    print(f"发现新版本 {remote}（当前 {local}），正在下载更新……")
    try:
        data = _get(f"https://codeload.github.com/{source}/zip/refs/heads/main", 60)
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        print(f"（下载更新失败：{e}，先用当前版本）")
        return 0

    old_req = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    names = zf.namelist()
    prefix = names[0].split("/")[0] + "/"
    for name in names:
        rel = name[len(prefix):]
        if not rel or name.endswith("/") or rel in KEEP:
            continue
        target = ROOT / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        content = zf.read(name)
        if rel == LAUNCHER:
            # 正在运行的 .bat 不能直接覆盖，先存成 .new，由启动器自己替换
            if not target.exists() or target.read_bytes() != content:
                (ROOT / (LAUNCHER + ".new")).write_bytes(content)
            continue
        target.write_bytes(content)

    if (ROOT / "requirements.txt").read_bytes() != old_req:
        (ROOT / ".deps_ok").unlink(missing_ok=True)  # 依赖变了，让启动器重新安装
    # 删除旧的 Python 缓存，避免用到旧代码
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    print(f"已更新到 {remote}")
    return 10


if __name__ == "__main__":
    sys.exit(main())
