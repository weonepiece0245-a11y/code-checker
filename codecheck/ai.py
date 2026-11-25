"""AI 分析：解释错误原因、给出修改建议和修改后的代码。

两种接法（auto 模式优先用 Claude Code 订阅）：
- api：用 Anthropic API key（环境变量 ANTHROPIC_API_KEY），按调用量计费。
- cli：调用本机已登录的 Claude Code（`claude -p`），用的是你的 Claude 订阅额度，不需要 API key。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager

from . import textio
from .proc import NEW_GROUP, NO_WINDOW, kill_tree

MODEL = os.environ.get("CODECHECK_MODEL", "claude-opus-5-5")

SYSTEM_PROMPT = """你是一位耐心、严谨的编程老师和代码审查员。
用户会给你：一段代码、静态检查结果、以及在沙箱里实际运行的结果（输出、报错、退出码）。
请用 {lang} 回答，面向可能是初学者的读者，避免堆砌术语；必须用术语时顺便解释一句。

按下面的 Markdown 结构输出，没有内容的小节写"无"：

## 总结
一两句话：这段代码能不能正常运行，最主要的问题是什么。

## 错误详解
对每个错误：在第几行、报错信息的意思、为什么会出错、怎么改。

## 潜在问题
运行时没暴露、但以后可能出错的地方（边界情况、空值、类型、性能、安全）。

## 改进建议
可读性、命名、结构上的建议，最多 5 条，按重要程度排序。

## 修改后的完整代码
给出修好之后的完整代码，放在一个代码块里，并在改动处加简短注释。

## 建议测试
列出 3~5 个值得测试的输入（含边界情况），以及预期结果。
"""


def fence(text: str, lang: str = "") -> str:
    """用比内容里最长的连续反引号还长的围栏包起来，内容里有 ``` 也不会提前结束。"""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    f = "`" * max(3, longest + 1)
    return f"{f}{lang}\n{text}\n{f}"


def _build_user_message(language_name: str, code: str, issues: list, run: dict | None, stdin: str) -> str:
    numbered = textio.number_lines(code)
    parts = [f"编程语言：{language_name}", f"代码（带行号）：\n{fence(numbered)}"]
    if issues:
        parts.append("静态检查发现：\n" + "\n".join(
            f"- 第 {i['line']} 行 [{i['code']}] {i['message']}" for i in issues))
    else:
        parts.append("静态检查发现：无")
    if run:
        parts.append(
            f"运行结果：退出码 {run['exit_code']}，耗时 {run['duration_ms']}ms"
            + ("，超时" if run["timed_out"] else "") + (f"，备注：{run['note']}" if run.get("note") else "")
            + (f"\n标准输入：\n{fence(stdin)}" if stdin else "")
            + f"\n标准输出：\n{fence(run['stdout'] or '（空）')}"
            + f"\n错误输出：\n{fence(run['stderr'] or '（空）')}")
    else:
        parts.append("运行结果：未运行")
    return "\n\n".join(parts)


def find_claude() -> str | None:
    """找 claude 命令：先查 PATH，再查常见安装位置（刚装完、PATH 还没刷新时也能找到）。"""
    exe = shutil.which("claude")
    if exe:
        return exe
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "claude.exe"),   # Windows 官方安装脚本
        os.path.join(home, ".local", "bin", "claude"),       # macOS / Linux 官方安装脚本
        os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),  # npm 安装
        os.path.join(home, ".claude", "local", "claude"),
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


NEED_AI = "这个功能需要把代码发给 AI，但你选了“不用 AI”。如果可以发送，请把 AI 改成“自动”再试。"


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def require_ai(choice: str) -> str:
    """AI 功能（生成测试、审查）的入口检查：用户明确选了“不用 AI”时不能偷偷改用 AI。"""
    if choice == "none":
        raise RuntimeError(NEED_AI)
    return choice


def pick_backend(choice: str = "auto") -> str:
    if choice != "auto":
        return choice
    # 优先用 Claude Code 订阅额度，没有 claude 命令才用 API key
    if find_claude():
        return "cli"
    if has_api_key():
        return "api"
    return "none"


@contextmanager
def _api_errors():
    """把 Anthropic API 的各种错误统一转成中文提示（_via_api 和 _structured_api 共用）。"""
    import anthropic
    try:
        yield
    except anthropic.AuthenticationError:
        raise RuntimeError("API key 无效，请检查 ANTHROPIC_API_KEY") from None
    except anthropic.RateLimitError:
        raise RuntimeError("请求太频繁或额度用完，请稍后再试") from None
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"API 出错（{e.status_code}）：{e.message}") from None
    except anthropic.APIConnectionError:
        raise RuntimeError("连不上 Anthropic API，请检查网络") from None


def _via_api(system: str, user: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    with _api_errors():
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium"},
            # 如果主模型因安全策略拒答，服务端自动换模型重试
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    if resp.stop_reason == "refusal":
        return "（AI 拒绝分析这段代码。）"
    text = "".join(b.text for b in resp.content if b.type == "text")
    if resp.stop_reason == "max_tokens":
        text += "\n\n（回答过长被截断）"
    return text


TRUNCATED_HINT = "AI 的回答太长，被截断了。可以排除一些文件夹、选一个子文件夹，或者分几次审查。"


def _run_claude(system: str, user: str, timeout: int, output_args: list[str], cancel=None) -> str:
    """调用本机已登录的 Claude Code（claude -p），返回它的标准输出。用的是订阅额度。"""
    exe = find_claude()  # Windows 上可能是 claude.cmd，需要完整路径
    if not exe:
        raise BackendUnavailable("没有找到 claude 命令，请先安装并登录 Claude Code")
    # 系统提示写进临时文件再传，避免 Windows 命令行对多行参数的限制
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(system)
        sys_file = f.name
    cmd = [exe, "-p", "--tools", "", "--system-prompt-file", sys_file, "--no-session-persistence", *output_args]
    if os.environ.get("CODECHECK_MODEL"):
        cmd += ["--model", MODEL]
    # 去掉 API key 环境变量，确保 Claude Code 走订阅额度而不是按 API 计费
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                creationflags=NO_WINDOW, encoding="utf-8", errors="replace", env=env, **NEW_GROUP)
        deadline, first = time.monotonic() + timeout, True
        while True:  # 每秒看一次：用户点了“取消”就立刻结束，不再继续消耗额度
            try:
                out, err = proc.communicate(user if first else None, timeout=1)
                break
            except subprocess.TimeoutExpired:
                first = False
                if (cancel is not None and cancel.is_set()) or time.monotonic() > deadline:
                    kill_tree(proc)  # 连同 claude 启动的子进程一起结束
                    try:
                        proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("已取消") from None
                    raise RuntimeError("Claude Code 响应超时") from None
        p = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    finally:
        try:
            os.unlink(sys_file)
        except OSError:
            pass
    if p.returncode != 0 and not p.stdout.strip().startswith("{"):
        raw = (p.stderr or p.stdout).strip()
        # 只看 Claude Code 自己输出的原始错误来判断是不是“没登录”，不能看下面拼接了提示的整句话
        error_type = BackendUnavailable if _is_login_error(raw) else RuntimeError
        raise error_type("Claude Code 调用失败：" + raw[:500] + "\n（如果提示未登录，请先在终端运行 claude 并登录）")
    return p.stdout


def _via_cli(system: str, user: str) -> str:
    return _run_claude(system, user, 300, ["--output-format", "text"]).strip()


class BackendUnavailable(RuntimeError):
    """Claude Code 本身用不了：没安装、没登录、登录失效、订阅不可用。只有这种错误才会改用 API key。"""


# 用来识别 Claude Code 原始错误信息里的“没登录”（英文为主；不包含我们自己附加的中文提示）
_LOGIN_HINTS = ("please run /login", "not logged in", "log in", "login required", "not authenticated",
                "authentication failed", "unauthorized", "invalid api key", "oauth token", "subscription")


def _is_login_error(message: str) -> bool:
    m = message.lower()
    return any(h in m for h in _LOGIN_HINTS)


def _with_fallback(choice: str, call_cli, call_api) -> tuple[str, object]:
    """按选择调用 AI。自动模式下 Claude Code 调用失败（没登录、订阅过期……）且设置了 API key 时，改用 API key 再试。"""
    backend = pick_backend(choice)
    if backend == "api":
        return "api", call_api()
    if backend == "cli":
        try:
            return "cli", call_cli()
        except InterruptedError:
            raise
        except BackendUnavailable:
            # 只有“Claude Code 本身用不了”时才改用 API key（会按量计费，结果里会标明用的是 API key）；
            # 其他错误（网络、额度用完、内容问题、回答被截断……）照常报出来，不悄悄换成付费的方式
            if choice == "auto" and has_api_key():
                return "api", call_api()
            raise
    if backend == "none":
        raise RuntimeError("这个功能需要 AI：请登录 Claude Code 或设置 API key")
    raise ValueError(f"未知的 AI 后端：{backend}")


def analyze(language_name: str, code: str, issues: list, run: dict | None, stdin: str = "",
            backend: str = "auto", explain_lang: str = "简体中文") -> tuple[str, str]:
    """返回 (使用的后端, 分析文本)。"""
    if backend == "none" or pick_backend(backend) == "none":
        return "none", ""
    system = SYSTEM_PROMPT.format(lang=explain_lang)
    user = _build_user_message(language_name, code, issues, run, stdin)
    return _with_fallback(backend, lambda: _via_cli(system, user), lambda: _via_api(system, user))


# ---------- 结构化输出（生成测试等功能用）----------

def _parse_object(text: str) -> dict:
    """把 AI 返回的文字解析成 dict；解析不了就给出看得懂的提示。"""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError("AI 返回的结果格式不对（可能被截断了）。请再试一次；"
                           "如果是审查文件夹，可以先排除一些文件夹。") from None
    if not isinstance(data, dict):
        raise RuntimeError("AI 返回的结果格式不对，请再试一次")
    return data


def _structured_api(system: str, user: str, schema: dict, timeout: int = 600, cancel=None) -> dict:
    import anthropic

    client = anthropic.Anthropic(timeout=timeout)
    with _api_errors():
        # 输出可能很长（审查整个文件夹），用流式请求避免超时，并放宽长度上限
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            for _ in stream:  # 边收边检查：用户点了“取消”就立刻断开，不再继续消耗额度
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("已取消")
            resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        raise RuntimeError("AI 拒绝处理这段代码")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(TRUNCATED_HINT)
    return _parse_object(next((b.text for b in resp.content if b.type == "text"), ""))


def _structured_cli(system: str, user: str, schema: dict, timeout: int = 400, cancel=None) -> dict:
    stdout = _run_claude(system, user, timeout,
                         ["--output-format", "json", "--json-schema", json.dumps(schema, ensure_ascii=False)], cancel)
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError("Claude Code 返回的内容无法解析：" + stdout.strip()[:300]) from None
    if not isinstance(out, dict):
        raise RuntimeError("Claude Code 返回的内容格式不对")
    if out.get("is_error"):
        result = str(out.get("result", ""))
        if "max" in str(out.get("stop_reason", "")) or "max_tokens" in result:
            raise RuntimeError(TRUNCATED_HINT)
        raise RuntimeError("Claude Code 出错：" + result[:500])
    if isinstance(out.get("structured_output"), dict):
        return out["structured_output"]
    if out.get("stop_reason") == "max_tokens":
        raise RuntimeError(TRUNCATED_HINT)
    return _parse_object(out.get("result"))


def structured(system: str, user: str, schema: dict, backend: str = "auto",
               timeout: int = 400, cancel=None) -> tuple[str, dict]:
    """让 AI 按 JSON Schema 返回结果。返回 (使用的后端, 数据)。cancel 被设置时会中途停止。"""
    require_ai(backend)
    return _with_fallback(backend,
                          lambda: _structured_cli(system, user, schema, timeout, cancel),
                          lambda: _structured_api(system, user, schema, timeout, cancel))
