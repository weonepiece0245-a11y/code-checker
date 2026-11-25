"""把需要长期记住的东西（排除的文件夹、上次的检查结果、上次的 AI 审查）存在程序文件夹里。

不用浏览器的 localStorage：桌面窗口默认是无痕模式，关掉程序就没了。
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIR = ROOT / ".userdata"
_lock = threading.Lock()


def _file(kind: str, folder: str) -> Path:
    key = hashlib.sha1(os.path.normcase(os.path.abspath(folder)).encode("utf-8")).hexdigest()[:16]
    return DIR / f"{kind}_{key}.json"


def get(kind: str, folder: str, default=None):
    f = _file(kind, folder)
    try:
        with _lock:
            return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # 文件不存在、损坏、编码不对：都当作没有记录
        return default


def put(kind: str, folder: str, value) -> None:
    f = _file(kind, folder)
    with _lock:
        DIR.mkdir(exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, f)


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")
