"""打开、保存本机文件，以及弹出“选择文件/文件夹”窗口。"""
import hashlib
import os
import shutil
import threading
import time
from pathlib import Path

from . import textio
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
    ct = textio.read(path)
    return {"path": path, "name": os.path.basename(path), "content": ct.text, "encoding": ct.encoding,
            "crlf": ct.crlf, "language": EXT.get(Path(path).suffix.lower())}


def save(path: str, content: str, encoding: str = "utf-8", crlf: bool = False) -> dict:
    path = os.path.abspath(path)
    _check_ext(path)
    if not os.path.isdir(os.path.dirname(path)):
        raise ValueError("文件夹不存在")
    backup = None
    if os.path.isfile(path):  # 覆盖前先备份到程序自己的 .backups 文件夹
        BACKUPS.mkdir(exist_ok=True)
        # 时间精确到毫秒，再加上所在文件夹的短编号：同一秒保存多次、不同文件夹里的同名文件都不会互相覆盖
        ms = int(time.time() * 1000) % 1000
        folder_id = hashlib.sha1(os.path.normcase(os.path.dirname(path)).encode("utf-8")).hexdigest()[:6]
        backup = BACKUPS / f"{time.strftime('%Y%m%d-%H%M%S')}-{ms:03d}_{folder_id}_{os.path.basename(path)}"
        n = 1
        while backup.exists():  # 极少数情况：同一毫秒内又保存了一次
            backup = backup.with_name(f"{backup.stem}-{n}{backup.suffix}")
            n += 1
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
    # 先完整写进同一文件夹里的临时文件，再一次性替换原文件：写到一半出错时原文件不受影响
    tmp = Path(path).with_name(f".{os.path.basename(path)}.{os.getpid()}.saving")
    try:
        tmp.write_bytes(data)
        if backup:
            shutil.copymode(backup, tmp)
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"保存失败，原文件没有被改动（{e}）") from None
    return {"ok": True, "path": path, "encoding": encoding, "backup": str(backup) if backup else None}
