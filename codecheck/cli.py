"""命令行用法：
    python -m codecheck 文件.py
    python -m codecheck 文件.js --input "1 2" --ai none
    python -m codecheck 文件.py --json        # 输出 JSON，方便给编辑器插件 / GitHub 机器人调用
"""
import argparse
import json
import os
import sys

from .core import check_code
from .languages import detect

C = {"red": "\033[31m", "yellow": "\033[33m", "green": "\033[32m", "cyan": "\033[36m",
     "bold": "\033[1m", "dim": "\033[2m", "end": "\033[0m"}


if sys.platform == "win32":
    os.system("")  # 让 Windows 命令行支持彩色输出


def color(s, c):
    return f"{C[c]}{s}{C['end']}" if sys.stdout.isatty() else s


EXPLAIN_LANGS = ["简体中文", "繁體中文", "English"]


def timeout_arg(v: str) -> int:
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError("--timeout 需要是 1~60 之间的整数") from None
    if not 1 <= n <= 60:
        raise argparse.ArgumentTypeError("--timeout 需要在 1~60 秒之间")
    return n


def read_text(path: str) -> str:
    """读取代码文件：找不到时给出清楚的提示；中文 Windows 上的 GBK 文件也能读。"""
    from . import textio
    try:
        return textio.read(path).text
    except FileNotFoundError:
        sys.exit(f"找不到文件：{path}")
    except IsADirectoryError:
        sys.exit(f"这是一个文件夹，不是文件：{path}")
    except PermissionError:
        sys.exit(f"没有权限读取：{path}")
    except OSError as e:
        sys.exit(f"读取文件失败：{path}（{e}）")


def indent_tail(text, max_lines=30):
    lines = text.rstrip().splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"...（还有 {len(lines) - max_lines} 行，已省略）"]
    return "\n".join("    " + ln for ln in lines)


def _ai(a) -> str:
    # 用户选了 --ai none 就是不想把代码发出去：AI 功能会给出提示，而不是偷偷改用 AI
    return a.ai


def _gen_tests(a, code: str) -> dict:
    """生成测试并运行；失败时返回 {"error": ...}。"""
    from . import testgen
    try:
        # 用和检查时一样的语言（看文件后缀），不要让生成测试时再从代码特征去猜
        lang_key = a.lang or detect(None if a.file == "-" else a.file, code).key
        g = testgen.generate(code, lang_key, a.explain_lang, _ai(a),
                             source_path=None if a.file == "-" else os.path.abspath(a.file))
        t = testgen.run(code, g["tests"], g["language"], a.timeout, a.sandbox,
                        source_path=None if a.file == "-" else os.path.abspath(a.file))
        return {"summary": g["summary"], **t}
    except Exception as e:
        return {"error": str(e)}


def _folder_mode(a) -> int:
    """检查整个文件夹。--json 时所有结果（包括 AI 审查/复查）合在一个 JSON 里输出。"""
    from . import folder, folder_ai
    r = folder.scan(a.file)
    if not a.json:
        print(folder.format_report(r))
    for flag, key, title, run, fmt in (
        (a.ai_recheck, "ai_recheck", "AI 复查：上次的问题解决了吗", folder_ai.recheck, folder_ai.format_recheck),
        (a.ai_review, "ai_review", "AI 审查整个文件夹", folder_ai.review, folder_ai.format_report),
    ):
        if not flag:
            continue
        if not a.json:
            print(color(f"\n━━ {title}（可能需要 1~3 分钟）━━", "bold"))
        def progress(done, total):
            if total > 1:
                print(f"  已完成 {done}/{total} 批", file=sys.stderr, flush=True)
        try:
            data = run(a.file, a.explain_lang, _ai(a), progress=progress)
            r[key] = data
            if not a.json:
                print(fmt(data))
        except Exception as e:
            r[key] = {"error": str(e)}
            if not a.json:
                print(color(f"{title}失败：" + str(e), "yellow"))
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    return 1 if r["with_errors"] else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="codecheck", description="检查代码、运行测试、AI 解释错误")
    p.add_argument("file", help="要检查的代码文件或文件夹，用 - 表示从标准输入读取")
    p.add_argument("--lang", choices=["python", "javascript"], help="编程语言（默认按后缀自动识别）")
    p.add_argument("--input", default="", help="运行时喂给程序的输入内容（写 \\n 表示换行）")
    p.add_argument("--input-raw", default=None, help="和 --input 一样，但原样传入，\\n 不会被当成换行")
    p.add_argument("--input-file", help="从文件读取运行时输入")
    p.add_argument("--no-run", action="store_true", help="只做静态检查，不运行")
    p.add_argument("--timeout", type=timeout_arg, default=5, help="运行超时秒数，1~60（默认 5）")
    p.add_argument("--sandbox", choices=["local", "docker"], default="local",
                   help="local=本机受限运行；docker=容器隔离运行（运行别人的代码时推荐）")
    p.add_argument("--ai", choices=["auto", "api", "cli", "none"], default="auto",
                   help="auto=优先用 Claude Code 订阅，没有才用 API key；none=不用 AI")
    p.add_argument("--explain-lang", default="简体中文", choices=EXPLAIN_LANGS, help="AI 讲解使用的语言（默认简体中文）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p.add_argument("--tests", action="store_true", help="让 AI 生成测试并逐个运行")
    p.add_argument("--ai-review", action="store_true", help="检查文件夹时，再让 AI 审查整个项目")
    p.add_argument("--ai-recheck", action="store_true", help="AI 复查：上次 AI 审查的问题解决了没有")
    a = p.parse_args(argv)

    if a.file != "-" and os.path.isdir(a.file):
        return _folder_mode(a)

    code = sys.stdin.read() if a.file == "-" else read_text(a.file)
    if a.input_file:
        stdin = read_text(a.input_file)
    elif a.input_raw is not None:
        stdin = a.input_raw
    else:
        stdin = a.input.replace("\\n", "\n")
    if not a.json and a.ai != "none":
        print(color("正在检查并请 AI 分析，可能需要几十秒……", "dim"), file=sys.stderr)
    r = check_code(code, None if a.file == "-" else a.file, a.lang, stdin, not a.no_run,
                   a.timeout, sandbox=a.sandbox, ai_backend=a.ai, explain_lang=a.explain_lang,
                   source_path=None if a.file == "-" else os.path.abspath(a.file))

    if a.json:
        if a.tests:
            r["tests"] = _gen_tests(a, code)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["passed"] else 1

    print(color(f"\n━━ 静态检查（{r['language']}）━━", "bold"))
    if not r["issues"]:
        print(color("  ✓ 没有发现问题", "green"))
    for i in r["issues"]:
        tag = color("错误", "red") if i["severity"] == "error" else color("警告", "yellow")
        print(f"  {tag} 第 {i['line']} 行 [{i['code']}] {i['message']}")

    if r["run_error"]:
        print(color("\n━━ 运行 ━━", "bold"))
        print(color("  无法运行：" + r["run_error"], "red"))
    elif r["run"]:
        x = r["run"]
        if x["ok"]:
            status = color("✓ 正常结束", "green")
        elif x["timed_out"]:
            status = color("✗ 超时", "red")
        else:
            status = color(f"✗ 失败（退出码 {x['exit_code']}）", "red")
        print(color("\n━━ 运行结果 ━━", "bold") + f"  {status}  {x['duration_ms']}ms  [{x['mode']}]")
        if x["note"]:
            print(color("  " + x["note"], "yellow"))
        if x["stdout"]:
            print(color("  输出：", "cyan"))
            print(indent_tail(x["stdout"]))
        if x["stderr"]:
            print(color("  错误输出：", "red"))
            print(indent_tail(x["stderr"]))

    if r["ai"]:
        label = {"api": "API key", "cli": "Claude Code 订阅"}.get(r["ai_backend"], r["ai_backend"])
        print(color(f"\n━━ AI 分析（{label}）━━", "bold"))
        print(r["ai"])
    elif r["ai_error"]:
        print(color("\nAI 分析失败：" + r["ai_error"], "yellow"))

    if a.tests:
        print(color("\n━━ AI 生成测试 ━━", "bold"))
        t = _gen_tests(a, code)
        if "error" in t:
            print(color("生成测试失败：" + t["error"], "yellow"))
        else:
            from . import testgen
            print("代码的用途：" + t["summary"])
            print(testgen.format_report(t))

    print(color("\n结论：通过 ✓", "green") if r["passed"] else color("\n结论：有问题 ✗", "red"))
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
