"""codecheck: 检查代码、安全运行、用 AI 解释错误并给出修改建议。"""
from pathlib import Path


def _read_version() -> str:
    # 版本号只写在 version.txt 一处（自动更新也读它），这里直接读，避免两处不一致
    try:
        return (Path(__file__).resolve().parent.parent / "version.txt").read_text(encoding="utf-8").strip() or "0"
    except OSError:
        return "0"


__version__ = _read_version()
