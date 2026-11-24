"""AI 自动生成测试，并在受限环境里逐个实际运行。

两种测试：
- call：先静默加载用户代码里的函数/类（顶层的打印和报错都忽略，Python 的
  `if __name__ == "__main__":` 部分跳过），再运行一小段调用代码，比较它打印出来的内容。
- program：把整个程序跑一遍，喂给它 stdin，比较全部输出。
"""
import json
import os
import tempfile

from . import ai, runner
from .languages import LANGUAGES, detect

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tests": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "kind": {"type": "string", "enum": ["call", "program"]},
                    "code": {"type": "string"},
                    "stdin": {"type": "string"},
                    "expected_output": {"type": "string"},
                    "expect_error": {"type": "boolean"},
                },
                "required": ["name", "purpose", "kind", "code", "stdin", "expected_output", "expect_error"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "tests"],
    "additionalProperties": False,
}

SYSTEM = """你是一位严谨的软件测试工程师。用户会给你一段 {language} 代码，请为它设计 5~8 个测试。

关键原则：expected_output 必须是【按代码的明显意图、正确实现时】应该得到的输出，
而不是这段代码（可能有 bug）实际会得到的输出。测试的目的就是把 bug 找出来。

测试的运行方式（请严格遵守）：
1. kind = "call"（代码里有函数或类时优先用这种）：
   系统会先静默加载用户代码中的函数、类和变量（顶层代码打印的内容会被丢弃、
   顶层报错会被忽略；Python 中 `if __name__ == "__main__":` 下面的内容会被跳过），
   然后运行你在 code 字段里写的调用代码。code 必须用 {print_fn} 把要检查的结果打印出来，
   系统比较的是 code 打印的全部内容。stdin 填空字符串。
2. kind = "program"（程序读取输入、或代码没有可调用的函数时用）：
   系统运行整个程序，把 stdin 字段作为标准输入，比较程序打印的全部内容。code 填空字符串。
   注意 Python 的 input("提示") 会把提示文字也打印出来，expected_output 要包含提示文字。

expected_output 要逐字精确，符合该语言真实的打印格式（例如 Python 的 print(4/2) 输出 2.0，
print([1, 2]) 输出 [1, 2]，print(None) 输出 None；JavaScript 的 console.log([1,2]) 输出 [ 1, 2 ]）。
多行输出用换行符分隔。末尾的空格和空行不影响比较。

expect_error：如果正确的行为就是抛出异常或报错退出（例如传入非法参数），设为 true，
此时 expected_output 写报错之前应该打印的内容（通常为空字符串）。

测试要覆盖：普通情况、边界情况（空输入、0、负数、单个元素、超长输入等）、以及容易出错的地方。
每个测试的 name 用一句简短的话描述（例如"空列表求平均值"），purpose 说明这个测试想验证什么。
name、purpose、summary 都用{explain_lang}写。summary 用一句话说明这段代码应该做什么。
"""

PY_HARNESS = r'''
import ast, contextlib, io, sys
user_path, test_path = sys.argv[1], sys.argv[2]
src = open(user_path, encoding="utf-8").read()
test = open(test_path, encoding="utf-8").read()
ns = {"__name__": "codecheck_test", "__builtins__": __builtins__}

def is_main_guard(node):
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__")

real_stdin = sys.stdin
sys.stdin = io.StringIO("")
for node in ast.parse(src).body:
    if is_main_guard(node):
        continue
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), ns)
    except BaseException:
        pass
sys.stdin = real_stdin
exec(compile(test, "test.py", "exec"), ns)
'''

JS_HARNESS = r'''
const vm = require("vm"), fs = require("fs");
const [userPath, testPath] = process.argv.slice(2);
const src = fs.readFileSync(userPath, "utf8"), test = fs.readFileSync(testPath, "utf8");
const quiet = { log() {}, error() {}, warn() {}, info() {}, debug() {}, table() {} };
const fakeProcess = Object.create(process);
fakeProcess.exit = () => { throw new Error("process.exit"); };
const mod = { exports: {} };
const ctx = vm.createContext({ require, module: mod, exports: mod.exports, Buffer, setTimeout,
  setInterval, clearTimeout, clearInterval, console: quiet, process: fakeProcess });
try { vm.runInContext(src, ctx, { filename: "main.js" }); } catch (e) {}
ctx.console = console;
ctx.process = process;
vm.runInContext(test, ctx, { filename: "test.js" });
'''

HARNESS = {"python": ("harness.py", PY_HARNESS), "javascript": ("harness.js", JS_HARNESS)}
PRINT_FN = {"python": "print()", "javascript": "console.log()"}


def generate(code: str, language: str | None = None, explain_lang: str = "简体中文",
             backend: str = "auto") -> dict:
    lang = LANGUAGES[language] if language else detect(None, code)
    numbered = "\n".join(f"{i + 1:>4} | {ln}" for i, ln in enumerate(code.splitlines()))
    system = SYSTEM.format(language=lang.name, print_fn=PRINT_FN[lang.key], explain_lang=explain_lang)
    used, data = ai.structured(system, f"代码（带行号，行号不是代码的一部分）：\n```\n{numbered}\n```",
                               SCHEMA, backend)
    data["language"] = lang.key
    data["ai_backend"] = used
    return data


def _norm(s: str) -> str:
    return "\n".join(ln.rstrip() for ln in s.replace("\r\n", "\n").strip("\n").split("\n")).rstrip()


def run(code: str, tests: list[dict], language: str | None = None, timeout: int = 5,
        sandbox: str = "local") -> dict:
    lang = LANGUAGES[language] if language else detect(None, code)
    results = []
    with tempfile.TemporaryDirectory(prefix="codecheck_t_") as tmp:
        os.chmod(tmp, 0o755)
        main_path = os.path.join(tmp, "main" + lang.suffix)
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(code)
        h_name, h_src = HARNESS[lang.key]
        h_path = os.path.join(tmp, h_name)
        with open(h_path, "w", encoding="utf-8") as f:
            f.write(h_src)
        test_path = os.path.join(tmp, "test" + lang.suffix)
        for t in tests:
            if t.get("kind") == "call":
                with open(test_path, "w", encoding="utf-8") as f:
                    f.write(t.get("code", ""))
                r = runner.run(lang, h_path, "", timeout, sandbox=sandbox,
                               extra_args=[os.path.basename(main_path), os.path.basename(test_path)])
            else:
                r = runner.run(lang, main_path, t.get("stdin", ""), timeout, sandbox=sandbox)
            actual, expected = _norm(r.stdout), _norm(t.get("expected_output", ""))
            errored = r.timed_out or r.exit_code != 0
            if r.timed_out:
                passed = False
            elif t.get("expect_error"):
                passed = errored and actual == expected
            else:
                passed = not errored and actual == expected
            stderr = r.stderr
            for p in (h_path, main_path, test_path):
                stderr = stderr.replace(p, os.path.basename(p))
            results.append({**t, "passed": passed, "error_line": error_line(stderr) if errored else "", "actual_output": r.stdout, "stderr": stderr[-3000:],
                            "exit_code": r.exit_code, "timed_out": r.timed_out,
                            "duration_ms": r.duration_ms})
    passed = sum(1 for x in results if x["passed"])
    return {"results": results, "passed": passed, "total": len(results)}


def error_line(stderr: str) -> str:
    """从报错信息里挑出最关键的一行（比如 TypeError: ...）。"""
    lines = [ln.strip() for ln in stderr.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        if "Error" in ln or "Exception" in ln:
            return ln
    return lines[-1] if lines else ""


def format_report(data: dict) -> str:
    """给命令行用的文字报告。"""
    lines = [f"共 {data['total']} 个测试，通过 {data['passed']} 个"]
    for i, r in enumerate(data["results"], 1):
        mark = "✓" if r["passed"] else "✗"
        lines.append(f"\n{mark} {i}. {r['name']}  —  {r['purpose']}")
        if not r["passed"]:
            what = r["code"] if r["kind"] == "call" else f"输入：{json.dumps(r['stdin'], ensure_ascii=False)}"
            lines.append("   测试：" + what.strip().replace("\n", "\n         "))
            lines.append("   期望：" + (r["expected_output"].strip() or "（无输出）")
                         + ("，并且应该报错" if r["expect_error"] else ""))
            lines.append("   实际：" + ("超时" if r["timed_out"] else (r["actual_output"].strip() or "（无输出）")))
            if r["stderr"].strip():
                lines.append("   报错：" + error_line(r["stderr"]))
    return "\n".join(lines)
