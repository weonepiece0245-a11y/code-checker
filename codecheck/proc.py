"""运行外部程序时的公共设置。

在 Windows 上，没有控制台的程序（pythonw）每启动一个命令行程序（python、node、ruff、claude……），
系统都会给它弹一个黑色窗口。加上 CREATE_NO_WINDOW 就不会弹了。
"""
import sys
from pathlib import Path

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


DETACHED = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED_PROCESS | NEW_PROCESS_GROUP


def windowless_python() -> str:
    """返回 pythonw.exe（没有黑色窗口）；没有的话就用当前的 Python。"""
    p = Path(sys.executable).with_name("pythonw.exe")
    return str(p) if p.exists() else sys.executable


def console_python() -> str:
    """返回 python.exe（而不是 pythonw.exe），用来运行代码和工具，输入输出行为和命令行里一致。"""
    p = Path(sys.executable)
    if p.name.lower() == "pythonw.exe" and p.with_name("python.exe").exists():
        return str(p.with_name("python.exe"))
    return sys.executable
