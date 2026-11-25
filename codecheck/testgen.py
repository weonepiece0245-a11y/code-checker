"""AI 自动生成测试，并在受限环境里逐个实际运行。

两种测试：
- call：先静默加载用户代码里的函数/类（顶层的打印和报错都忽略，Python 的
  `if __name__ == "__main__":` 部分跳过），再运行一小段调用代码，比较它打印出来的内容。
- program：把整个程序跑一遍，喂给它 stdin，比较全部输出。
"""
import json
import os
import re
import tempfile

from . import ai, runner, textio
from .languages import LANGUAGES, SUFFIX, code_suffix, detect

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
import __future__, ast, contextlib, io, os, sys, types
user_path, test_path = sys.argv[1], sys.argv[2]
source = sys.argv[3] if len(sys.argv) > 3 else ""
src = open(user_path, encoding="utf-8").read()
test = open(test_path, encoding="utf-8").read()
fname = source or "main.py"
if source:  # 和正常检查一样：能 import 原文件夹里的其他模块
    sys.path.insert(0, os.path.dirname(os.path.abspath(source)))

# 用户代码放进真正的 __main__ 模块：pickle、dataclasses 等按 __main__ 找名字的功能才正常
mod = types.ModuleType("__main__")
mod.__file__ = source or os.path.abspath(user_path)
mod.__builtins__ = __builtins__
sys.modules["__main__"] = mod
ns = mod.__dict__

def is_main_guard(node):
    # if __name__ == "__main__":、if "__main__" == __name__:、if __name__ == "__main__" and ...: 都算
    if not isinstance(node, ast.If):
        return False
    for sub in ast.walk(node.test):
        if isinstance(sub, ast.Compare):
            parts = [sub.left, *sub.comparators]
            if any(isinstance(p, ast.Name) and p.id == "__name__" for p in parts) and \
                    any(isinstance(p, ast.Constant) and p.value == "__main__" for p in parts):
                return True
    return False

# 一条一条地运行顶层语句（某条出错不影响后面的函数定义），顶层的输出全部丢掉。
# from __future__ 的设置要带到后面每一条语句的编译里，否则会失效
flags = 0
load_errors = []
real_stdin = sys.stdin
sys.stdin = io.StringIO("")
for node in ast.parse(src, fname).body:
    if is_main_guard(node):
        continue
    if isinstance(node, ast.ImportFrom) and node.module == "__future__":
        for alias in node.names:
            feature = getattr(__future__, alias.name, None)
            if feature is not None:
                flags |= feature.compiler_flag
        continue
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            code = compile(ast.Module(body=[node], type_ignores=[]), fname, "exec", flags=flags, dont_inherit=True)
            exec(code, ns)
    except BaseException as e:
        # 顶层代码出错不影响后面的函数定义；但记下来（最多 3 条），测试失败时告诉用户可能的原因
        if len(load_errors) < 3 and not isinstance(e, (EOFError, SystemExit, KeyboardInterrupt)):
            load_errors.append(f"第 {node.lineno} 行：{type(e).__name__}: {e}")
sys.stdin = real_stdin
for msg in load_errors:
    sys.stderr.write("[codecheck-load] " + msg + "\n")
exec(compile(test, "test.py", "exec", dont_inherit=True), ns)
'''

JS_HARNESS = r'''
const vm = require("vm"), fs = require("fs"), path = require("path"), Module = require("module");
const [userPath, testPath, sourceArg] = process.argv.slice(2);
const source = sourceArg || path.resolve(userPath);
const src = fs.readFileSync(userPath, "utf8"), test = fs.readFileSync(testPath, "utf8");

// 加载用户代码时：所有输出都丢掉；定时器只登记不执行（避免顶层的 setTimeout/setInterval 在测试时打印或让程序不退出）
const quiet = { log() {}, error() {}, warn() {}, info() {}, debug() {}, table() {}, dir() {}, trace() {} };
const sink = { write() { return true; }, isTTY: false };
const fakeProcess = Object.create(process);
Object.defineProperty(fakeProcess, "stdout", { value: sink });
Object.defineProperty(fakeProcess, "stderr", { value: sink });
fakeProcess.exit = () => { throw new Error("process.exit"); };
const fakeTimer = () => { const t = { ref: () => t, unref: () => t, hasRef: () => false, refresh: () => t }; return t; };
const noop = () => {};

// 和正常检查一样：require 按原文件的位置找模块，__filename / __dirname 指向原文件
const mod = new Module(source, null);
mod.filename = source;
mod.paths = Module._nodeModulePaths(path.dirname(source));
const ctx = vm.createContext({
  require: Module.createRequire(source), module: mod, exports: mod.exports,
  __filename: source, __dirname: path.dirname(source),
  Buffer, URL, URLSearchParams, TextEncoder, TextDecoder, queueMicrotask, structuredClone,
  setTimeout: fakeTimer, setInterval: fakeTimer, setImmediate: fakeTimer,
  clearTimeout: noop, clearInterval: noop, clearImmediate: noop,
  console: quiet, process: fakeProcess,
});
try { vm.runInContext(src, ctx, { filename: source }); } catch (e) {
  // 不悄悄吞掉：告诉用户加载代码时出了什么错（比如 ES 模块的 import 语法这里不支持）
  process.stderr.write("[codecheck-load] " + String(e && e.message || e).split("\n")[0] + "\n");
}

// 等加载阶段排队的 Promise 回调跑完（仍然是静音状态），再恢复真正的 console 等，运行测试
setImmediate(() => {
  Object.assign(ctx, { console, process, setTimeout, setInterval, setImmediate, clearTimeout, clearInterval, clearImmediate });
  vm.runInContext(test, ctx, { filename: "test.js" });
});
'''

HARNESS = {"python": ("harness.py", PY_HARNESS), "javascript": ("harness.js", JS_HARNESS)}
PRINT_FN = {"python": "print()", "javascript": "console.log()"}


# 测试代码里出现这些操作，就先不运行，交给用户确认（防止被检查的代码里藏着“提示注入”，
# 诱导 AI 写出删除文件、联网、执行命令的测试）
_RISKY = {
    "python": [
        (r"\bos\.(remove|unlink|rmdir|removedirs|rename|replace|system|popen|kill|chmod|chown|exec\w*|spawn\w*)\b", "删除/改动文件或执行命令"),
        (r"\bshutil\.(rmtree|move|copy\w*|chown)\b", "删除/移动/复制文件"),
        (r"\b(subprocess|ctypes|importlib|pty|signal)\b", "执行外部命令或调用系统底层"),
        (r"\b(socket|urllib|requests|httpx|http\.client|ftplib|smtplib|telnetlib|webbrowser)\b", "联网"),
        (r"\b(eval|exec|compile|__import__)\s*\(", "动态执行代码"),
        (r"\bopen\s*\([^)]*['\"][^'\"]*[wax+][^'\"]*['\"]", "写入文件"),
        (r"\.(write_text|write_bytes|unlink|rmdir|rename|replace|touch|mkdir|chmod)\s*\(", "写入/删除文件"),
    ],
    "javascript": [
        (r"\bchild_process\b|\b(exec|execSync|spawn|spawnSync|fork)\s*\(", "执行外部命令"),
        (r"\bfs\w*\.(unlink|rm|rmdir|writeFile|appendFile|rename|copyFile|mkdir|chmod|symlink|truncate)\w*\s*\(", "写入/删除文件"),
        (r"require\s*\(\s*['\"](fs|fs/promises|child_process|net|http|https|dgram|tls|os|vm|worker_threads)['\"]\s*\)", "使用文件、网络或系统模块"),
        (r"\bprocess\.(kill|exit|chdir)\b", "操作进程"),
        (r"\b(eval|Function)\s*\(|\bfetch\s*\(|\bXMLHttpRequest\b|\bWebSocket\b", "动态执行代码或联网"),
    ],
}


def risk_of(test: dict, lang_key: str) -> str:
    """测试代码里有没有危险操作；有的话返回说明，没有返回空字符串。"""
    code = test.get("code") or ""
    found = [why for pat, why in _RISKY.get(lang_key, []) if re.search(pat, code)]
    return "、".join(dict.fromkeys(found))


def _siblings(source_path: str | None) -> str:
    """原文件旁边还有哪些代码文件：告诉 AI，免得它以为 import 的模块不存在、设计出错误的测试。"""
    if not source_path or not os.path.isfile(source_path):
        return ""
    folder = os.path.dirname(os.path.abspath(source_path))
    me = os.path.basename(source_path)
    try:
        names = sorted(n for n in os.listdir(folder)
                       if n != me and os.path.splitext(n)[1].lower() in SUFFIX and os.path.isfile(os.path.join(folder, n)))
        dirs = sorted(n for n in os.listdir(folder)
                      if os.path.isdir(os.path.join(folder, n)) and not n.startswith((".", "__")))
    except OSError:
        return ""
    if not names and not dirs:
        return ""
    more = f"（还有 {len(names) - 60} 个没列出）" if len(names) > 60 else ""
    return ("\n\n这个文件所在的文件夹里还有这些文件和子文件夹，测试运行时可以正常 import / require 它们，"
            "不要假设它们不存在：\n" + "、".join(names[:60] + [d + "/" for d in dirs[:30]]) + more)


def _is_esm(code: str, source_path: str | None) -> bool:
    if source_path and source_path.lower().endswith(".mjs"):
        return True
    return bool(re.search(r"^\s*(import\s.+\sfrom\s|import\s+['\"]|export\s)", code, re.M))


def generate(code: str, language: str | None = None, explain_lang: str = "简体中文",
             backend: str = "auto", source_path: str | None = None) -> dict:
    lang = LANGUAGES[language] if language else detect(source_path, code)
    numbered = textio.number_lines(code)
    system = SYSTEM.format(language=lang.name, print_fn=PRINT_FN[lang.key], explain_lang=explain_lang)
    user = f"代码（带行号，行号不是代码的一部分）：\n{ai.fence(numbered)}" + _siblings(source_path)
    if lang.key == "javascript" and _is_esm(code, source_path):
        user += ("\n\n注意：这段代码是 ES 模块（用了 import/export），系统没法单独加载里面的函数，"
                 "所以【只能】设计 kind = \"program\" 的测试（运行整个程序、比较输出）。")
    used, data = ai.structured(system, user, SCHEMA, backend)
    data["language"] = lang.key
    data["ai_backend"] = used
    return data


def _norm(s: str) -> str:
    return "\n".join(ln.rstrip() for ln in s.replace("\r\n", "\n").strip("\n").split("\n")).rstrip()


def run(code: str, tests: list[dict], language: str | None = None, timeout: int = 5,
        sandbox: str = "local", source_path: str | None = None, allow_risky: bool = False) -> dict:
    """source_path：代码对应的真实文件。给了它，测试就和正常检查一样在原位置运行（能 import 同目录模块）。"""
    lang = LANGUAGES[language] if language else detect(source_path, code)
    if sandbox == "docker" or (source_path and not os.path.isfile(source_path)):
        source_path = None  # Docker 容器里看不到原文件夹
    results = []
    with tempfile.TemporaryDirectory(prefix="codecheck_t_") as tmp:
        if sandbox == "docker":
            os.chmod(tmp, 0o755)  # 只有 Docker 里的程序需要读
        main_path = os.path.join(tmp, "main" + code_suffix(source_path, lang))
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(code)
        h_name, h_src = HARNESS[lang.key]
        h_path = os.path.join(tmp, h_name)
        with open(h_path, "w", encoding="utf-8") as f:
            f.write(h_src)
        test_path = os.path.join(tmp, "test" + lang.suffix)
        for t in tests:
            risk = risk_of(t, lang.key)
            if risk and not allow_risky:
                results.append({**t, "passed": False, "skipped": True, "risk": risk, "error_line": "",
                                "actual_output": "", "stderr": "", "exit_code": None, "timed_out": False,
                                "duration_ms": 0, "load_errors": []})
                continue
            if t.get("kind") == "call":
                with open(test_path, "w", encoding="utf-8") as f:
                    f.write(t.get("code", ""))
                if source_path:  # 在原文件夹里运行：临时文件要用完整路径
                    args = [main_path, test_path, os.path.abspath(source_path)]
                else:            # 在临时文件夹（或 Docker 的 /code）里运行：用文件名
                    args = [os.path.basename(main_path), os.path.basename(test_path)]
                r = runner.run(lang, h_path, "", timeout, sandbox=sandbox, extra_args=args,
                               workdir=os.path.dirname(source_path) if source_path else None)
            else:
                r = runner.run(lang, main_path, t.get("stdin", ""), timeout, sandbox=sandbox,
                               source_path=source_path)
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
            # 测试运行器记下的“加载代码时出错”单独拿出来，不和测试本身的报错混在一起
            load_errors = [ln.split("] ", 1)[1] for ln in stderr.splitlines() if ln.startswith("[codecheck-load] ")]
            stderr = "\n".join(ln for ln in stderr.splitlines() if not ln.startswith("[codecheck-load] "))
            results.append({**t, "passed": passed, "skipped": False, "risk": risk,
                            "error_line": error_line(stderr) if errored else "",
                            "actual_output": r.stdout, "stderr": stderr[-3000:], "load_errors": load_errors,
                            "exit_code": r.exit_code, "timed_out": r.timed_out, "duration_ms": r.duration_ms})
    ran = [x for x in results if not x.get("skipped")]
    return {"results": results, "passed": sum(1 for x in ran if x["passed"]), "total": len(ran),
            "skipped": len(results) - len(ran)}


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
    if data.get("skipped"):
        lines.append(f"另有 {data['skipped']} 个测试里有危险操作（删除文件、联网、执行命令等），为了安全没有运行；"
                     "看过测试代码、确认没问题的话，加 --allow-risky-tests 再运行")
    for i, r in enumerate(data["results"], 1):
        if r.get("skipped"):
            lines.append(f"\n⚠ {i}. {r['name']}（没有运行：测试里有{r['risk']}的操作）")
            lines.append("   测试：" + r["code"].strip().replace("\n", "\n         "))
            continue
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
            for le in r.get("load_errors") or []:
                lines.append("   加载代码时：" + le)
    return "\n".join(lines)
