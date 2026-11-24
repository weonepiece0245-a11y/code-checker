"""静态检查：不运行代码，直接找语法错误和潜在问题。"""
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict


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
    return [sys.executable, "-m", "ruff"]


def check_python(path: str) -> list[Issue]:
    issues: list[Issue] = []
    # 1) 语法检查
    proc = subprocess.run([sys.executable, "-I", "-m", "py_compile", path],
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
    rules = "E9,F,B,E711,E712,E722,W605"
    try:
        r = subprocess.run(_ruff_cmd() + ["check", "--isolated", "--select", rules,
                                          "--output-format", "json", "--no-cache", path],
                           capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        for item in json.loads(r.stdout or "[]"):
            code = item.get("code")
            if not code or code == "invalid-syntax":  # 语法错误上面已经报过了
                continue
            sev = "error" if code in ERROR_RULES or code.startswith("F7") else "warning"
            issues.append(Issue(item["location"]["row"], item["location"]["column"],
                                item["code"], item["message"], sev))
    except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
        issues.append(Issue(0, 0, "tool", "没有找到 ruff，跳过深度检查（pip install ruff）", "warning"))
    return issues


def check_javascript(path: str) -> list[Issue]:
    issues: list[Issue] = []
    if not shutil.which("node"):
        return [Issue(0, 0, "tool", "没有安装 Node.js，无法检查 JavaScript", "warning")]
    proc = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=20,
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
        r = subprocess.run([shutil.which("eslint"), "--format", "json", path], capture_output=True, text=True,
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
