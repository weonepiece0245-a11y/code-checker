"""支持的编程语言（被检查的代码是用什么语言写的）。"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    key: str          # 内部名称
    name: str         # 显示名称
    suffix: str       # 文件后缀
    run_cmd: tuple    # 运行命令，{file} 会被替换成代码文件路径
    docker_image: str


LANGUAGES = {
    "python": Language("python", "Python", ".py", ("python3", "-I", "{file}"), "python:3.12-slim"),
    "javascript": Language("javascript", "JavaScript", ".js",
                           ("node", "--max-old-space-size=256", "{file}"), "node:22-slim"),
}

_SUFFIX = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript"}


_JS = [r"^\s*(const|let|var)\s+[\w${}\[\], ]+\s*=", r"\bfunction\b\s*[\w$]*\s*\(", r"=>", r"console\.\w+\(",
       r"===|!==", r";\s*$", r"\brequire\(", r"^\s*export\s", r"^\s*import\s.+\sfrom\s+['\"]",
       r"^\s*import\s+['\"]", r"^\s*}\s*(else\b.*)?$", r"\{\s*$", r"\bnew\s+[A-Z]\w*\(", r"//"]
_PY = [r"^\s*def\s+\w+\s*\(.*\)\s*(->.*)?:\s*(#.*)?$", r"^\s*(if|elif|else|for|while|try|except|finally|with|class)\b.*:\s*(#.*)?$",
       r"^\s*import\s+[\w.]+(\s+as\s+\w+)?\s*$", r"^\s*from\s+[\w.]+\s+import\s", r"\bself\.", r"\bNone\b|\bTrue\b|\bFalse\b",
       r"^\s*print\(", r"^\s*#", r"\belif\b", r"\blambda\b", r"f\"|f'"]
_JS_RE = [re.compile(p, re.M) for p in _JS]
_PY_RE = [re.compile(p, re.M) for p in _PY]


def _score(code: str, patterns) -> int:
    return sum(len(p.findall(code)) for p in patterns)


def detect(filename: str | None, code: str) -> Language:
    """先看文件后缀；没有后缀就看代码能不能被 Python 编译，再结合两种语言的特征来判断。"""
    if filename:
        for suf, key in _SUFFIX.items():
            if filename.lower().endswith(suf):
                return LANGUAGES[key]
    try:
        compile(code, "<detect>", "exec")  # 只编译，不运行
        py_ok = True
    except (SyntaxError, ValueError, RecursionError, MemoryError, OverflowError):
        py_ok = False
    js, py = _score(code, _JS_RE), _score(code, _PY_RE)
    if py_ok and js <= py:
        return LANGUAGES["python"]
    if not py_ok and js > 0:
        return LANGUAGES["javascript"]
    return LANGUAGES["javascript" if js > py else "python"]
