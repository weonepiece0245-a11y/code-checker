"""打开、保存本机文件，以及弹出“选择文件/文件夹”窗口。"""
import os
import shutil
import threading
import time
from pathlib import Path

from .folder import EXT

ROOT = Path(__file__).resolve().parent.parent
BACKUPS = ROOT / ".backups"
MAX_SIZE = 1_000_000
ALLOWED = set(EXT) | {".txt"}

# 桌面应用会把这个换成 pywebview 的原生对话框；否则用 tkinter
PICKER = None
_lock = threading.Lock()


def _tk_pick(kind: str) -> str | None:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "folder":
            p = filedialog.askdirectory(title="选择要检查的文件夹", parent=root)
        else:
            p = filedialog.askopenfilename(
                title="选择代码文件", parent=root,
                filetypes=[("代码文件", "*.py *.js *.mjs *.cjs"), ("所有文件", "*.*")])
    finally:
        root.destroy()
    return os.path.normpath(p) if p else None


def pick(kind: str) -> str | None:
    with _lock:  # 同一时间只弹一个对话框
        if PICKER:
            return PICKER(kind)
        return _tk_pick(kind)


def _check_ext(path: str):
    if Path(path).suffix.lower() not in ALLOWED:
        raise ValueError("只能打开 .py / .js / .mjs / .cjs 文件")


def read(path: str) -> dict:
    path = os.path.abspath(path)
    _check_ext(path)
    if not os.path.isfile(path):
        raise ValueError("文件不存在")
    if os.path.getsize(path) > MAX_SIZE:
        raise ValueError("文件太大（超过 1MB）")
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):  # 中文 Windows 上老文件常是 GBK
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        enc, text = "utf-8", data.decode("utf-8", errors="replace")
    return {"path": path, "name": os.path.basename(path), "content": text.replace("\r\n", "\n"),
            "encoding": "utf-8" if enc == "utf-8-sig" and not data.startswith(b"\xef\xbb\xbf") else enc,
            "crlf": b"\r\n" in data, "language": EXT.get(Path(path).suffix.lower())}


def save(path: str, content: str, encoding: str = "utf-8", crlf: bool = False) -> dict:
    path = os.path.abspath(path)
    _check_ext(path)
    if not os.path.isdir(os.path.dirname(path)):
        raise ValueError("文件夹不存在")
    backup = None
    if os.path.isfile(path):  # 覆盖前先备份到程序自己的 .backups 文件夹
        BACKUPS.mkdir(exist_ok=True)
        backup = BACKUPS / f"{time.strftime('%Y%m%d-%H%M%S')}_{os.path.basename(path)}"
        shutil.copy2(path, backup)
        olds = sorted(BACKUPS.iterdir())
        for old in olds[:-100]:  # 只保留最近 100 份
            old.unlink(missing_ok=True)
    text = content.replace("\r\n", "\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    if encoding not in ("utf-8", "utf-8-sig", "gbk"):
        encoding = "utf-8"
    try:
        data = text.encode(encoding)
    except UnicodeEncodeError:  # GBK 存不下的字符，改存 UTF-8
        data, encoding = text.encode("utf-8"), "utf-8"
    Path(path).write_bytes(data)
    return {"ok": True, "path": path, "encoding": encoding, "backup": str(backup) if backup else None}
