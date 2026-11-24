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

C = {"red": "\033[31m", "yellow": "\033[33m", "green": "\033[32m", "cyan": "\033[36m",
     "bold": "\033[1m", "dim": "\033[2m", "end": "\033[0m"}


if sys.platform == "win32":
    os.system("")  # 让 Windows 命令行支持彩色输出


def color(s, c):
    return f"{C[c]}{s}{C['end']}" if sys.stdout.isatty() else s


def indent_tail(text, max_lines=30):
    lines = text.rstrip().splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"...（还有 {len(lines) - max_lines} 行，已省略）"]
    return "\n".join("    " + ln for ln in lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="codecheck", description="检查代码、运行测试、AI 解释错误")
    p.add_argument("file", help="要检查的代码文件，用 - 表示从标准输入读取")
    p.add_argument("--lang", choices=["python", "javascript"], help="编程语言（默认按后缀自动识别）")
    p.add_argument("--input", default="", help="运行时喂给程序的输入内容")
    p.add_argument("--input-file", help="从文件读取运行时输入")
    p.add_argument("--no-run", action="store_true", help="只做静态检查，不运行")
    p.add_argument("--timeout", type=int, default=5, help="运行超时秒数（默认 5）")
    p.add_argument("--sandbox", choices=["local", "docker"], default="local",
                   help="local=本机受限运行；docker=容器隔离运行（运行别人的代码时推荐）")
    p.add_argument("--ai", choices=["auto", "api", "cli", "none"], default="auto",
                   help="auto=优先用 Claude Code 订阅，没有才用 API key；none=不用 AI")
    p.add_argument("--explain-lang", default="简体中文", help="AI 讲解使用的语言（默认简体中文）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    a = p.parse_args(argv)

    code = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    stdin = open(a.input_file, encoding="utf-8").read() if a.input_file else a.input.replace("\\n", "\n")
    if not a.json and a.ai != "none":
        print(color("正在检查并请 AI 分析，可能需要几十秒……", "dim"), file=sys.stderr)
    r = check_code(code, None if a.file == "-" else a.file, a.lang, stdin, not a.no_run,
                   a.timeout, sandbox=a.sandbox, ai_backend=a.ai, explain_lang=a.explain_lang)

    if a.json:
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

    print(color("\n结论：通过 ✓", "green") if r["passed"] else color("\n结论：有问题 ✗", "red"))
    return 0 if r["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
