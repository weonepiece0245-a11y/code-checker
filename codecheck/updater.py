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
    from . import read_version  # 每次都重新读文件：更新后不用重启也能拿到新版本号
    return read_version()


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
        _install(staged, files)
        new_req = (ROOT / "requirements.txt").read_bytes() if (ROOT / "requirements.txt").exists() else b""
    if new_req != old_req:
        (ROOT / ".deps_ok").unlink(missing_ok=True)  # 依赖变了，让启动器重新安装
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


MANIFEST = ".manifest"  # 记录哪些文件是更新器装进来的：只有它们才会在新版本删除时被删掉


def _read_manifest() -> set[str]:
    try:
        lines = (ROOT / MANIFEST).read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    out = set()
    for rel in lines:
        rel = rel.strip()
        pp = PurePosixPath(rel)
        if rel and not pp.is_absolute() and ".." not in pp.parts and "\\" not in rel and ":" not in rel:
            out.add(rel)
    return out


def _install(staged: Path, files: dict) -> None:
    """把临时文件夹里检查好的新版本装进程序文件夹。

    要么全部成功，要么全部还原：覆盖前先备份旧文件，中途任何一步失败（文件被占用、磁盘满……）
    就把已经改动的文件全部恢复、新增的文件删掉，程序保持原来能用的样子。
    """
    old_manifest = _read_manifest()
    if not old_manifest:
        # 还没有安装清单（第一次用这个版本的更新器）：codecheck/ 是程序自己的文件夹，
        # 里面新版本已经没有的 .py 文件就是被删掉的模块
        pkg = ROOT / "codecheck"
        old_manifest = {p.relative_to(ROOT).as_posix() for p in pkg.rglob("*.py")
                        if "__pycache__" not in p.parts} if pkg.is_dir() else set()
    removed = sorted(old_manifest - set(files) - KEEP - {LAUNCHER, MANIFEST})
    backup_dir = Path(tempfile.mkdtemp(prefix="codecheck_rollback_"))
    changed: list[tuple[Path, Path | None]] = []  # (程序里的文件, 它的备份；None 表示原来没有这个文件)
    new_launcher = ROOT / (LAUNCHER + ".new")

    def backup(target: Path, rel: str):
        if target.is_file():
            b = backup_dir / rel
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, b)
            changed.append((target, b))
        else:
            changed.append((target, None))

    try:
        for rel in files:
            src, target = staged / rel, ROOT / rel
            if rel == LAUNCHER:
                # 正在运行的 .bat 不能直接覆盖，先存成 .new，由启动器自己替换
                if not target.exists() or target.read_bytes() != src.read_bytes():
                    backup(new_launcher, LAUNCHER + ".new")
                    shutil.copyfile(src, new_launcher)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            backup(target, rel)
            tmp_target = target.with_name(target.name + ".updating")
            shutil.copyfile(src, tmp_target)
            tmp_target.replace(target)  # 单个文件原子替换，不会出现写了一半的文件
        for rel in removed:  # 新版本里已经删掉的文件（只删更新器以前装进来的）
            target = (ROOT / rel).resolve()
            if target.is_file() and target.is_relative_to(ROOT.resolve()):
                backup(target, rel)
                target.unlink()
        manifest = ROOT / MANIFEST
        backup(manifest, MANIFEST)
        tmp_manifest = manifest.with_name(MANIFEST + ".updating")
        tmp_manifest.write_text("\n".join(sorted(files)) + "\n", encoding="utf-8")
        tmp_manifest.replace(manifest)
    except BaseException:
        for target, b in reversed(changed):
            try:
                if b is None:
                    target.unlink(missing_ok=True)
                else:
                    tmp = target.with_name(target.name + ".restoring")
                    shutil.copy2(b, tmp)
                    tmp.replace(target)
            except OSError:
                pass
        for leftover in ROOT.rglob("*.updating"):
            leftover.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(backup_dir, ignore_errors=True)


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
