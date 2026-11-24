"""检查并安装需要的组件。核心组件装不上就失败；桌面窗口组件装不上也能用（改用 Edge 应用模式）。"""
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ["fastapi", "uvicorn", "anthropic", "ruff", "pydantic"]
WEBVIEW_FAILED = ROOT / ".webview_failed"
from .proc import NO_WINDOW, console_python


def _pip(args: list, quiet: bool) -> tuple[int, str]:
    cmd = [console_python(), "-m", "pip", "install", "--disable-pip-version-check", *args]
    if not quiet:
        return subprocess.call(cmd), ""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NO_WINDOW)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def ensure(progress=print, quiet: bool = False) -> tuple[bool, str]:
    """返回 (是否成功, 失败时的输出)。progress 用来显示当前在做什么。"""
    if any(find_spec(m) is None for m in CORE) or not (ROOT / ".deps_ok").exists():
        progress("正在安装需要的组件，第一次大约 1 分钟……")
        code, out = _pip(["-r", str(ROOT / "requirements.txt")], quiet)
        if code != 0:
            return False, out
        (ROOT / ".deps_ok").write_text("ok")
    if find_spec("webview") is None and not WEBVIEW_FAILED.exists():
        progress("正在安装桌面窗口组件……")
        code, _ = _pip(["pywebview>=5"], quiet)
        if code != 0:
            WEBVIEW_FAILED.write_text("1")  # 不影响使用，下次不再重试
    return True, ""


def main() -> int:
    ok, _ = ensure()
    if not ok:
        print("安装失败，请把上面的错误信息截图发给 Claude。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
