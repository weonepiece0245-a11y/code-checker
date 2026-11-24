"""静态检查：不运行代码，直接找语法错误和潜在问题。"""
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, asdict

from .proc import NO_WINDOW, console_python


@dataclass
class Issue:
    line: int
    col: int
    code: str      # 规则编号，如 F821
    message: str
    severity: str  # error / warning

    def to_dict(self):
        return asdict(self)


# 这些规则一旦触发，运行时基本一定会出错
ERROR_RULES = {"F821", "F822", "F823", "F632", "F811", "F601", "F502", "F503", "F506", "F507"}


def _ruff_cmd():
    if shutil.which("ruff"):
        return ["ruff"]
    return [console_python(), "-m", "ruff"]


def check_python(path: str) -> list[Issue]:
    issues: list[Issue] = []
    # 1) 语法检查
    proc = subprocess.run([console_python(), "-I", "-m", "py_compile", path], creationflags=NO_WINDOW,
                          capture_output=True, text=True, timeout=20,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        line = 0
        for part in proc.stderr.split(","):
            part = part.strip()
            if part.startswith("line "):
                try:
                    line = int(part.split()[1].rstrip(")"))
                except ValueError:
                    pass
        msg = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "语法错误"
        issues.append(Issue(line, 0, "SyntaxError", msg, "error"))
    # 2) ruff：常见 bug（未定义变量、未使用导入、可变默认参数等）
    try:
        issues += ruff_many([path], include_syntax=False).get(path, [])
    except RuntimeError as e:
        issues.append(Issue(0, 0, "tool", str(e), "warning"))
    return issues


RUFF_RULES = "E9,F,B,E711,E712,E722,W605"

# 常见规则的中文说明（显示为“中文说明（英文原文）”）
ZH = {
    "F821": "用了一个没有定义的名字，可能是拼写错误或忘了定义/导入",
    "F822": "__all__ 里列出了不存在的名字",
    "F823": "在给局部变量赋值之前就使用了它",
    "F401": "导入了但没有使用",
    "F811": "重复定义了同一个名字，前面的定义被覆盖了",
    "F841": "变量赋值后从来没有用过",
    "F632": "用 is 比较数字或字符串，应该用 ==",
    "F601": "字典里有重复的键",
    "F541": "f 字符串里没有 {} 占位符",
    "F403": "使用了 from ... import *，很难知道导入了什么",
    "F405": "这个名字可能来自 import *，无法确定是否定义",
    "F704": "yield 用在了函数外面",
    "F706": "return 用在了函数外面",
    "E711": "和 None 比较应该用 is None / is not None",
    "E712": "和 True/False 比较应该直接写条件，或用 is",
    "E722": "不要用空的 except:，它会把所有错误（包括按 Ctrl+C）都吞掉",
    "E902": "读取文件失败",
    "W605": "字符串里有无效的转义字符，可能需要用 r\"...\" 原始字符串",
    "B006": "默认参数不要用列表、字典等可变对象，多次调用会共用同一个对象",
    "B007": "循环变量在循环体里没有用到",
    "B008": "默认参数里调用了函数，它只会在定义时执行一次",
    "B011": "不要用 assert False，加 -O 运行时会被跳过",
    "B018": "这一行的表达式没有任何作用",
    "B904": "在 except 里重新抛出异常时，建议用 raise ... from err",
}


def _zh(code: str, message: str) -> str:
    return f"{ZH[code]}（{message}）" if code in ZH else message


def _ruff_json(r: subprocess.CompletedProcess) -> list:
    """解析 ruff 的输出。ruff 没装好、崩溃时不能当成“没有问题”，要明确报错。"""
    out = (r.stdout or "").strip()
    if r.returncode not in (0, 1) or not out.startswith("["):
        detail = (r.stderr or out or "").strip().splitlines()
        hint = detail[-1][:200] if detail else f"退出码 {r.returncode}"
        if "No module named ruff" in (r.stderr or ""):
            raise RuntimeError("没有安装 ruff，跳过了深度检查（可以运行 pip install ruff 安装）")
        raise RuntimeError(f"ruff 运行失败，跳过了深度检查（{hint}）")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError("ruff 的输出无法解析，跳过了深度检查") from None


def ruff_text(text: str, filename: str, include_syntax: bool = True) -> list[Issue]:
    """检查一段文本（用于 GBK 等非 UTF-8 文件：先按正确编码解码，再交给 ruff）。"""
    try:
        r = subprocess.run(creationflags=NO_WINDOW,
                           args=_ruff_cmd() + ["check", "--isolated", "--select", RUFF_RULES,
                                               "--output-format", "json", "--no-cache",
                                               "--stdin-filename", filename, "-"],
                           input=text, capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        items = _ruff_json(r)
    except FileNotFoundError:
        raise RuntimeError("没有找到 ruff，跳过了深度检查（可以运行 pip install ruff 安装）") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("ruff 检查超时，跳过了深度检查") from None
    return [i for i in (_to_issue(item, include_syntax) for item in items) if i]


def _to_issue(item: dict, include_syntax: bool) -> Issue | None:
    code = item.get("code")
    if not code or code == "invalid-syntax":
        if not include_syntax:
            return None
        msg = item["message"].removeprefix("SyntaxError:").strip()
        return Issue(item["location"]["row"], item["location"]["column"], "SyntaxError",
                     f"语法错误，代码写法不对，程序无法运行（{msg}）", "error")
    sev = "error" if code in ERROR_RULES or code.startswith("F7") else "warning"
    return Issue(item["location"]["row"], item["location"]["column"], code, _zh(code, item["message"]), sev)


def ruff_many(paths: list[str], include_syntax: bool = True) -> dict[str, list[Issue]]:
    """用 ruff 一次检查多个 Python 文件，返回 {文件路径: 问题列表}。"""
    result: dict[str, list[Issue]] = {}
    norm = {os.path.normcase(os.path.abspath(p)): p for p in paths}
    for i in range(0, len(paths), 150):  # 分批，避免 Windows 命令行过长
        batch = paths[i:i + 150]
        try:
            r = subprocess.run(creationflags=NO_WINDOW,
                               args=_ruff_cmd() + ["check", "--isolated", "--select", RUFF_RULES,
                                                   "--output-format", "json", "--no-cache", *batch],
                               capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
            items = _ruff_json(r)
        except FileNotFoundError:
            raise RuntimeError("没有找到 ruff，跳过了深度检查（可以运行 pip install ruff 安装）") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError("ruff 检查超时，跳过了深度检查") from None
        for item in items:
            key = norm.get(os.path.normcase(os.path.abspath(item["filename"])), item["filename"])
            if item.get("code") == "E902":  # 不是 UTF-8（多半是 GBK），换个编码再查
                try:
                    with open(key, "rb") as f:
                        text = f.read().decode("gbk")
                    result.setdefault(key, []).extend(ruff_text(text, key, include_syntax))
                    continue
                except (OSError, UnicodeDecodeError):
                    pass
            issue = _to_issue(item, include_syntax)
            if issue:
                result.setdefault(key, []).append(issue)
    return result


def check_javascript(path: str) -> list[Issue]:
    issues: list[Issue] = []
    if not shutil.which("node"):
        return [Issue(0, 0, "tool", "没有安装 Node.js，无法检查 JavaScript", "warning")]
    proc = subprocess.run(["node", "--check", path], creationflags=NO_WINDOW, capture_output=True, text=True, timeout=20,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        line = 0
        first = proc.stderr.splitlines()[0] if proc.stderr else ""
        if ":" in first:
            try:
                line = int(first.rsplit(":", 1)[1])
            except ValueError:
                pass
        msg = next((ln for ln in proc.stderr.splitlines() if "Error" in ln), proc.stderr.strip())
        issues.append(Issue(line, 0, "SyntaxError", msg.strip(), "error"))
    # 如果项目里装了 eslint 就顺便用上
    if shutil.which("eslint"):
        r = subprocess.run(creationflags=NO_WINDOW, args=[shutil.which("eslint"), "--format", "json", path], capture_output=True, text=True,
                           timeout=60, encoding="utf-8", errors="replace", env={**os.environ, "ESLINT_USE_FLAT_CONFIG": "true"})
        try:
            for f in json.loads(r.stdout or "[]"):
                for m in f.get("messages", []):
                    issues.append(Issue(m.get("line", 0), m.get("column", 0), m.get("ruleId") or "eslint",
                                        m["message"], "error" if m.get("severity") == 2 else "warning"))
        except json.JSONDecodeError:
            pass
    return issues


def check(lang_key: str, path: str) -> list[Issue]:
    if lang_key == "python":
        return check_python(path)
    if lang_key == "javascript":
        return check_javascript(path)
    return []
