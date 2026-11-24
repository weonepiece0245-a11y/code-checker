"""运行外部程序时的公共设置。

在 Windows 上，没有控制台的程序（pythonw）每启动一个命令行程序（python、node、ruff、claude……），
系统都会给它弹一个黑色窗口。加上 CREATE_NO_WINDOW 就不会弹了。
"""
import sys
from pathlib import Path

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def console_python() -> str:
    """返回 python.exe（而不是 pythonw.exe），用来运行代码和工具，输入输出行为和命令行里一致。"""
    p = Path(sys.executable)
    if p.name.lower() == "pythonw.exe" and p.with_name("python.exe").exists():
        return str(p.with_name("python.exe"))
    return sys.executable
