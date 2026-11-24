"""支持的编程语言（被检查的代码是用什么语言写的）。"""
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


def detect(filename: str | None, code: str) -> Language:
    """先看文件后缀，没有后缀就根据代码内容猜。"""
    if filename:
        for suf, key in _SUFFIX.items():
            if filename.endswith(suf):
                return LANGUAGES[key]
    js_hints = ("console.log", "function ", "const ", "let ", "=>", "require(")
    py_hints = ("def ", "print(", "import ", "elif ", "self.")
    js = sum(h in code for h in js_hints)
    py = sum(h in code for h in py_hints)
    return LANGUAGES["javascript" if js > py else "python"]
