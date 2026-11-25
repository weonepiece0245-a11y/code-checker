"""没有黑色窗口的启动器（用 pythonw 运行）。

顺序：替换新版启动文件 → 刷新桌面快捷方式 → 检查更新 → 安装组件 → 打开应用窗口。
过程中显示一个小的启动画面；出错时弹出提示框。
"""
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from .proc import DETACHED, NO_WINDOW, windowless_python

ROOT = Path(__file__).resolve().parent.parent
BAT = ROOT / "启动网页版.bat"
ICON = ROOT / "app.ico"
IS_WIN = sys.platform == "win32"
pythonw = windowless_python


def spawn(module: str):
    subprocess.Popen([pythonw(), "-m", module], cwd=ROOT, creationflags=DETACHED,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=not IS_WIN)


def refresh_shortcut():
    """桌面快捷方式直接指向 pythonw + 启动器，这样双击时不会出现黑色窗口。"""
    if not IS_WIN:
        return
    try:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "create_shortcut.ps1"), "-PythonW", pythonw()],
                       cwd=ROOT, creationflags=NO_WINDOW, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class Splash:
    """启动画面。没有 tkinter 时退化成打印。"""

    def __init__(self):
        self.q = queue.Queue()
        self.root = None
        if os.environ.get("CODECHECK_NO_SPLASH"):
            return
        try:
            import tkinter as tk
            from tkinter import ttk
        except Exception:
            return
        try:
            root = tk.Tk()
        except Exception:
            return
        root.title("代码检查助手")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        w, h = 380, 130
        root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 2}")
        frame = tk.Frame(root, bg="#2563eb", padx=2, pady=2)
        frame.pack(fill="both", expand=True)
        inner = tk.Frame(frame, bg="white", padx=20, pady=16)
        inner.pack(fill="both", expand=True)
        tk.Label(inner, text="代码检查助手", bg="white", fg="#1d2129",
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        self.label = tk.Label(inner, text="正在启动……", bg="white", fg="#6b7280",
                              font=("Microsoft YaHei UI", 10))
        self.label.pack(anchor="w", pady=(6, 8))
        bar = ttk.Progressbar(inner, mode="indeterminate", length=w - 50)
        bar.pack(anchor="w")
        bar.start(12)
        try:
            root.iconbitmap(str(ICON))
        except Exception:
            pass
        self.root = root

    def set(self, text: str):
        self.q.put(("set", text))

    def error(self, text: str):
        self.q.put(("error", text))

    def close(self):
        self.q.put(("close", None))

    def run(self, work):
        threading.Thread(target=self._wrap, args=(work,), daemon=True).start()
        if not self.root:
            while True:
                kind, text = self.q.get()
                if kind == "close":
                    return
                print(("出错：" if kind == "error" else "") + text)
        self._poll()
        self.root.mainloop()

    def _wrap(self, work):
        try:
            work(self)
        except Exception as e:
            self.error(f"启动失败：{e}")
        self.close()

    def _poll(self):
        try:
            while True:
                kind, text = self.q.get_nowait()
                if kind == "set":
                    self.label.config(text=text)
                elif kind == "error":
                    from tkinter import messagebox
                    self.root.withdraw()
                    messagebox.showerror("代码检查助手", text + "\n\n请把这个提示截图发给 Claude。")
                elif kind == "close":
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


def work(ui: Splash):
    # 1. 自动更新留下的新版启动文件，现在没人在运行它，可以直接替换
    new_bat = ROOT / (BAT.name + ".new")
    if new_bat.exists():
        os.replace(new_bat, BAT)
    refresh_shortcut()

    # 2. 检查更新
    from . import updater
    ui.set("正在检查更新……")
    info = updater.check()
    if info["has_update"]:
        ui.set(f"正在更新到 {info['remote']}……")
        try:
            updater.apply(updater.update_source(), info["remote"])
            spawn("codecheck.launcher")  # 用新版本的代码重新启动
            return
        except Exception as e:
            ui.set(f"更新失败（{e}），先用当前版本……")

    # 3. 组件
    from . import deps
    ok, out = deps.ensure(progress=ui.set, quiet=True)
    if not ok:
        tail = "\n".join(out.strip().splitlines()[-8:])
        ui.error("安装组件失败：\n\n" + tail)
        return

    # 4. 打开窗口
    ui.set("正在打开窗口……")
    from . import app
    status, log_tail = app.spawn_and_wait()
    if status == "failed":
        ui.error("窗口打开失败。日志最后几行：\n\n" + log_tail)


def main() -> int:
    if "--console" in sys.argv:  # 调试用：在命令行里看每一步
        os.environ["CODECHECK_NO_SPLASH"] = "1"
    Splash().run(work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
