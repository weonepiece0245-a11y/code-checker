"""检查并安装需要的组件。核心组件装不上就失败；桌面窗口组件装不上也能用（改用 Edge 应用模式）。"""
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ["fastapi", "uvicorn", "anthropic", "ruff", "pydantic"]
WEBVIEW_FAILED = ROOT / ".webview_failed"


def _pip(*args) -> int:
    return subprocess.call([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args])


def main() -> int:
    if any(find_spec(m) is None for m in CORE) or not (ROOT / ".deps_ok").exists():
        print("正在安装需要的组件，第一次会慢一点……")
        if _pip("-r", str(ROOT / "requirements.txt")) != 0:
            print("安装失败，请把上面的错误信息截图发给 Claude。")
            return 1
        (ROOT / ".deps_ok").write_text("ok")
    if find_spec("webview") is None and not WEBVIEW_FAILED.exists():
        print("正在安装桌面窗口组件……")
        if _pip("pywebview>=5") != 0:
            print("桌面窗口组件安装失败，将改用 Edge 应用窗口（不影响使用）。")
            WEBVIEW_FAILED.write_text("1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
