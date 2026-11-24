"""桌面应用：在独立窗口里打开，不用浏览器。

    python -m codecheck.app            直接打开窗口
    python -m codecheck.app --detach   在后台启动窗口并立刻返回（启动器用，这样黑色窗口可以关掉）

窗口用 pywebview（Windows 上是系统自带的 Edge WebView2）。如果 pywebview 用不了，
会改用 Edge 的“应用模式”打开一个没有地址栏的独立窗口；再不行才用普通浏览器。
关闭窗口后程序自动退出。日志写在 app.log。
"""
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "app.ico"
READY = ROOT / ".app_ready"
LOG = ROOT / "app.log"
TITLE = "代码检查助手"

LOADING_HTML = """<!doctype html><meta charset="utf-8"><body style="margin:0;height:100vh;display:flex;
align-items:center;justify-content:center;font:16px system-ui,'Microsoft YaHei';background:#f6f7f9;color:#555">
<div>正在启动代码检查助手……</div></body>"""


def _log_to_file():
    """pythonw 没有控制台，把输出写到 app.log 方便排查问题。"""
    if sys.stdout is None or sys.stderr is None or "--log" in sys.argv:
        try:
            if LOG.exists() and LOG.stat().st_size > 1_000_000:
                LOG.unlink()
            f = open(LOG, "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stderr = f
        except OSError:
            pass
    print(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 ====")


def _free_port(preferred: int = 8765) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def _start_server(port: int) -> str:
    import uvicorn

    from .web import server
    server.RESTART_HOOK = restart
    config = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
    threading.Thread(target=uvicorn.Server(config).run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            urllib.request.urlopen(url + "/api/info", timeout=1)
            return url
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("内部服务启动失败")


def _mark_ready():
    try:
        READY.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def restart():
    """一键更新后调用：用无窗口启动器重新启动（它会完成替换文件、安装组件），然后退出自己。"""
    print("更新完成，重新启动")
    from .launcher import spawn
    spawn("codecheck.launcher")
    os._exit(0)


def _set_icon(window):
    """把窗口和任务栏图标换成 app.ico（只在 Windows 上）。"""
    if sys.platform != "win32" or not ICON.exists():
        return
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        hwnd = window.native.Handle.ToInt64()
        for size, which in ((256, 1), (32, 0)):  # ICON_BIG=1, ICON_SMALL=0
            hicon = user32.LoadImageW(None, str(ICON), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            if hicon:
                user32.SendMessageW(hwnd, 0x80, which, hicon)  # WM_SETICON
    except Exception as e:
        print("设置图标失败：", e)


def _find_edge() -> str | None:
    if sys.platform != "win32":
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return None


def _run_webview(port: int) -> bool:
    try:
        import webview
    except ImportError:
        print("没有安装 pywebview")
        return False
    try:
        win = webview.create_window(TITLE, html=LOADING_HTML, width=1320, height=900,
                                    min_size=(900, 600), text_select=True)
        win.events.shown += lambda: _set_icon(win)

        def picker(kind: str):
            FD = getattr(webview, "FileDialog", None)
            if kind == "folder":
                r = win.create_file_dialog(FD.FOLDER if FD else webview.FOLDER_DIALOG)
            else:
                dlg = FD.OPEN if FD else webview.OPEN_DIALOG
                try:
                    r = win.create_file_dialog(dlg, file_types=("Code files (*.py;*.js;*.mjs;*.cjs)",
                                                                "All files (*.*)"))
                except Exception:
                    r = win.create_file_dialog(dlg)
            if not r:
                return None
            return r[0] if isinstance(r, (list, tuple)) else r

        from . import files
        files.PICKER = picker

        def boot():
            try:
                win.load_url(_start_server(port))
                _mark_ready()
            except Exception as e:
                print("启动失败：", e)
                win.load_html(f"<body style='font:16px system-ui;padding:40px'>启动失败：{e}"
                              "<br><br>请把这个窗口截图发给 Claude。</body>")
                _mark_ready()

        try:
            webview.start(boot, private_mode=False, storage_path=str(ROOT / ".userdata" / "webview"))
        except TypeError:  # 旧版 pywebview 没有这些参数
            webview.start(boot)
        return True
    except Exception as e:
        print("窗口模式启动失败：", repr(e))
        return False


def _run_fallback(port: int):
    """没有 pywebview 时：Edge 应用模式（或浏览器），页面关闭一段时间后自动退出。"""
    from .web import server
    url = _start_server(port)
    server.WATCHDOG = True
    edge = _find_edge()
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=1320,900"])
    else:
        webbrowser.open(url)
    _mark_ready()
    started = time.time()
    while True:
        time.sleep(3)
        last = server.LAST_PING
        if (not last and time.time() - started > 180) or (last and time.time() - last > 180):
            print("页面已关闭，退出")
            os._exit(0)


def spawn_and_wait(timeout: float = 60) -> tuple[str, str]:
    """在后台启动窗口，等它真正打开再返回。返回 ("ok" | "failed" | "slow", 日志最后几行)。"""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw) if pyw.exists() else sys.executable
    READY.unlink(missing_ok=True)
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED_PROCESS | NEW_GROUP
    p = subprocess.Popen([exe, "-m", "codecheck.app"], cwd=ROOT, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=sys.platform != "win32")
    for _ in range(int(timeout * 10)):
        if READY.exists():
            return "ok", ""
        if p.poll() is not None:
            try:
                tail = "\n".join(LOG.read_text(encoding="utf-8").splitlines()[-15:])
            except OSError:
                tail = "（没有日志）"
            return "failed", tail
        time.sleep(0.1)
    return "slow", ""


def _detach() -> int:
    status, tail = spawn_and_wait()
    if status == "failed":
        print("窗口启动失败，日志：\n" + tail)
        return 2
    return 0


def main() -> int:
    if "--detach" in sys.argv:
        return _detach()
    _log_to_file()
    if sys.platform == "win32":
        try:  # 让任务栏把它当成独立的应用（显示自己的图标）
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CodeCheck.Desktop")
        except Exception:
            pass
    port = _free_port()
    if not _run_webview(port):
        _run_fallback(port)
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
