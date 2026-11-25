"""读取代码文件的公共工具：自动识别 UTF-8 / GBK 编码，统一换行符。"""
from dataclasses import dataclass
from pathlib import Path

# 中文 Windows 上的老文件常是 GBK；按这个顺序尝试
ENCODINGS = ("utf-8", "gbk")


@dataclass
class CodeText:
    text: str        # 换行统一成 \n
    encoding: str    # utf-8 / utf-8-sig（带 BOM）/ gbk
    crlf: bool       # 原文件是不是 Windows 换行（\r\n）


def decode(data: bytes) -> CodeText:
    crlf = b"\r\n" in data
    if data.startswith(b"\xef\xbb\xbf"):
        return CodeText(data[3:].decode("utf-8", errors="replace").replace("\r\n", "\n"), "utf-8-sig", crlf)
    for enc in ENCODINGS:
        try:
            return CodeText(data.decode(enc).replace("\r\n", "\n"), enc, crlf)
        except UnicodeDecodeError:
            continue
    return CodeText(data.decode("utf-8", errors="replace").replace("\r\n", "\n"), "utf-8", crlf)


def number_lines(text: str) -> str:
    """给代码每一行加上行号（发给 AI 用），行号和编辑器里看到的一致。全项目只用这一份。"""
    lines = text.replace("\r\n", "\n").split("\n")
    if len(lines) > 1 and lines[-1] == "":  # 末尾的换行不算新的一行
        lines.pop()
    return "\n".join(f"{n:>4} | {ln}" for n, ln in enumerate(lines, 1))


def read(path: str | Path) -> CodeText:
    """读取并解码；文件不存在等错误照常抛出 OSError，由调用方决定怎么提示。"""
    with open(path, "rb") as f:
        return decode(f.read())
