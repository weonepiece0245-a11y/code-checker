"""启动时自动检查并下载新版本。

更新来源写在项目根目录的 update_source.txt 里，格式是 GitHub 的 "用户名/仓库名"。
文件为空就不检查更新。网络不通、下载失败都会直接跳过，不影响正常使用。

退出码：0 = 没有更新或已跳过；10 = 更新完成，需要重新启动启动器。
"""
import io
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

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


def update_source() -> str:
    f = ROOT / "update_source.txt"
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


def local_version() -> str:
    f = ROOT / "version.txt"
    try:
        return f.read_text(encoding="utf-8").strip() or "0"
    except OSError:
        return "0"


def remote_version(source: str) -> str:
    """先用 GitHub API（没有缓存延迟），失败再用 raw 地址（可能有 5 分钟缓存）。"""
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{source}/contents/version.txt",
            headers={"User-Agent": "codecheck-updater", "Accept": "application/vnd.github.raw"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return _get(f"https://raw.githubusercontent.com/{source}/main/version.txt", 5).decode().strip()


def check() -> dict:
    """给网页用：返回 {local, remote, has_update, error}。"""
    source = update_source()
    local = local_version()
    if not source:
        return {"local": local, "remote": None, "has_update": False, "error": "没有设置更新地址"}
    try:
        remote = remote_version(source)
    except Exception:
        return {"local": local, "remote": None, "has_update": False, "error": "检查更新失败，可能没联网"}
    return {"local": local, "remote": remote, "has_update": _ver(remote) > _ver(local), "error": ""}


REQUIRED = ("version.txt", "codecheck/__init__.py", "codecheck/updater.py", "requirements.txt")


def _safe_rel(name: str, prefix: str) -> str | None:
    """压缩包里的路径必须是项目内部的普通相对路径（防止 ../ 或绝对路径写到别处，即 Zip Slip）。"""
    if not name.startswith(prefix):
        return None
    rel = name[len(prefix):]
    if not rel or rel.endswith("/"):
        return None
    pp = PurePosixPath(rel)
    if pp.is_absolute() or ".." in pp.parts or "\\" in rel or ":" in rel:
        raise ValueError(f"更新包里有不安全的路径：{name}")
    target = (ROOT / rel).resolve()
    if not target.is_relative_to(ROOT.resolve()):
        raise ValueError(f"更新包里的文件会写到程序文件夹外面：{name}")
    return rel


def apply(source: str, remote: str) -> None:
    """下载 → 在临时文件夹里解压并检查 → 全部没问题再覆盖。失败时抛出异常，原文件不动。"""
    data = _get(f"https://codeload.github.com/{source}/zip/refs/heads/main", 60)
    zf = zipfile.ZipFile(io.BytesIO(data))
    if zf.testzip() is not None:
        raise ValueError("更新包已损坏")
    names = zf.namelist()
    prefix = names[0].split("/")[0] + "/"
    files = {}
    for name in names:
        rel = _safe_rel(name, prefix)
        if rel is not None and rel not in KEEP:
            files[rel] = name

    with tempfile.TemporaryDirectory(prefix="codecheck_update_") as tmp:
        staged = Path(tmp)
        for rel, name in files.items():
            dest = staged / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(name))
        # 检查：版本号要和刚才查到的一致，必要的文件都要在，Python 文件要能编译
        missing = [r for r in REQUIRED if not (staged / r).is_file()]
        if missing:
            raise ValueError("更新包不完整，缺少：" + "、".join(missing))
        got = (staged / "version.txt").read_text(encoding="utf-8").strip()
        if got != remote:
            raise ValueError(f"更新包的版本（{got}）和预期（{remote}）不一致，可能正在发布中，请稍后再试")
        for py in (staged / "codecheck").rglob("*.py"):  # 只检查程序本身；examples 里有故意写错的示例
            compile(py.read_text(encoding="utf-8"), str(py), "exec")

        old_req = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
        for rel in files:
            src, target = staged / rel, ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel == LAUNCHER:
                # 正在运行的 .bat 不能直接覆盖，先存成 .new，由启动器自己替换
                if not target.exists() or target.read_bytes() != src.read_bytes():
                    shutil.copyfile(src, ROOT / (LAUNCHER + ".new"))
                continue
            tmp_target = target.with_name(target.name + ".updating")
            shutil.copyfile(src, tmp_target)
            tmp_target.replace(target)  # 单个文件原子替换，不会出现写了一半的文件
        new_req = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    if new_req != old_req:
        (ROOT / ".deps_ok").unlink(missing_ok=True)  # 依赖变了，让启动器重新安装
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def main() -> int:
    try:
        return _main()
    except Exception as e:  # 更新出任何问题都不能影响启动
        print(f"（自动更新出错：{e}，先用当前版本）")
        return 0


def _main() -> int:
    info = check()
    if info["error"]:
        if info["remote"] is None and update_source():
            print("（" + info["error"] + "，先用当前版本）")
        return 0
    if not info["has_update"]:
        print(f"当前已是最新版本 {info['local']}")
        return 0
    print(f"发现新版本 {info['remote']}（当前 {info['local']}），正在下载更新……")
    try:
        apply(update_source(), info["remote"])
    except Exception as e:
        print(f"（下载更新失败：{e}，先用当前版本）")
        return 0
    print(f"已更新到 {info['remote']}")
    return 10


if __name__ == "__main__":
    sys.exit(main())
