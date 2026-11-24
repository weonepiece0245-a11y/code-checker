"""AI 审查整个文件夹：把项目里的代码（加上静态检查结果）交给 AI，找逻辑错误、安全隐患等。"""
import os

from . import ai, folder

CHAR_BUDGET = 150_000  # 一次最多发送多少字符的代码（大约 5 万~8 万 token）

SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "category": {"type": "string",
                                 "enum": ["bug", "security", "performance", "error_handling",
                                          "structure", "readability"]},
                    "title": {"type": "string"},
                    "explanation": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["file", "line", "severity", "category", "title", "explanation", "suggestion"],
                "additionalProperties": False,
            },
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "issues", "strengths", "next_steps"],
    "additionalProperties": False,
}

SYSTEM = """你是一位经验丰富、说话直接但友善的代码审查员，读者可能是编程初学者。
用户会给你一个项目文件夹里的多个代码文件（每行前面有行号），以及自动静态检查已经发现的问题。

请审查整个项目，重点找静态检查发现不了的问题，按重要程度：
1. bug：逻辑错误、边界情况没处理（空值、空列表、除以零、越界）、会导致崩溃或结果错误的地方
2. security：安全隐患（执行用户输入、SQL 拼接、硬编码密码/密钥、不安全的文件路径等）
3. error_handling：该处理却没处理的错误（文件不存在、网络失败、输入格式不对）
4. performance：明显的性能问题
5. structure：文件之间的结构问题（重复代码、职责混乱、互相依赖）
6. readability：严重影响阅读的命名或写法

要求：
- issues 最多 15 条，按严重程度从高到低排列；宁缺毋滥，不要为了凑数报告不重要的问题。
- 静态检查已经报告的“导入未使用”之类小问题不要重复，除非它暴露了更大的问题。
- file 必须和给出的文件路径完全一致；line 用给出的行号，不确定就填 0。
- title 一句话概括；explanation 说明为什么是问题、会造成什么后果；suggestion 给出具体怎么改（可以带简短代码）。
- overview 用两三句话说明这个项目是做什么的、整体质量如何。
- strengths 列出 1~3 个写得好的地方；next_steps 列出 3 条最值得先做的改进。
- 全部用{explain_lang}回答。
"""


def plan(path: str, scan_result: dict | None = None) -> dict:
    """决定发送哪些文件：有错误的优先，然后是有警告的，再按文件大小从小到大，直到用完额度。"""
    r = scan_result or folder.scan(path)
    included, skipped, used = [], [], 0
    order = sorted(r["files"], key=lambda f: (-f["errors"], -f["warnings"], f["lines"]))
    for f in order:
        try:
            size = os.path.getsize(f["path"])
        except OSError:
            continue
        if used + size > CHAR_BUDGET:
            skipped.append(f["rel"])
            continue
        included.append(f)
        used += size
    return {"scan": r, "included": included, "skipped": sorted(skipped), "chars": used}


def _read(path: str) -> str:
    data = open(path, "rb").read()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def review(path: str, explain_lang: str = "简体中文", backend: str = "auto") -> dict:
    p = plan(path)
    if not p["included"]:
        raise ValueError("这个文件夹里没有可以审查的代码文件")
    parts = [f"项目文件夹：{os.path.basename(p['scan']['folder'])}，共 {p['scan']['total']} 个代码文件，"
             f"这次发送了其中 {len(p['included'])} 个。"]
    if p["skipped"]:
        parts.append("因为篇幅限制没有发送的文件：" + "、".join(p["skipped"][:50]))
    for f in sorted(p["included"], key=lambda f: f["rel"].lower()):
        text = _read(f["path"]).replace("\r\n", "\n")
        numbered = "\n".join(f"{i + 1:>4} | {ln}" for i, ln in enumerate(text.split("\n")))
        lint = "\n".join(f"  第 {i['line']} 行 [{i['code']}] {i['message']}" for i in f["issues"]) or "  无"
        parts.append(f"===== 文件：{f['rel']} =====\n静态检查发现：\n{lint}\n代码：\n{numbered}")
    used, data = ai.structured(SYSTEM.format(explain_lang=explain_lang), "\n\n".join(parts), SCHEMA,
                               backend, timeout=900)

    # 把 AI 说的相对路径对应回真实文件，方便界面里点击打开
    by_rel = {f["rel"].replace("\\", "/").lower(): f["path"] for f in p["included"]}
    for issue in data["issues"]:
        issue["path"] = by_rel.get(issue["file"].replace("\\", "/").lstrip("./").lower())
    order = {"high": 0, "medium": 1, "low": 2}
    data["issues"].sort(key=lambda i: order.get(i["severity"], 3))
    data.update(folder=p["scan"]["folder"], reviewed=[f["rel"] for f in p["included"]],
                skipped=p["skipped"], ai_backend=used)
    return data


SEV = {"high": "🔴 严重", "medium": "🟠 中等", "low": "🟡 轻微"}
CAT = {"bug": "逻辑错误", "security": "安全", "performance": "性能", "error_handling": "错误处理",
       "structure": "结构", "readability": "可读性"}


def format_report(d: dict) -> str:
    lines = ["项目概况：" + d["overview"], f"审查了 {len(d['reviewed'])} 个文件"
             + (f"，{len(d['skipped'])} 个因为篇幅限制没有审查" if d["skipped"] else "")]
    for n, i in enumerate(d["issues"], 1):
        where = i["file"] + (f" 第 {i['line']} 行" if i["line"] else "")
        lines += [f"\n{n}. {SEV.get(i['severity'], '')} [{CAT.get(i['category'], '')}] {i['title']}",
                  f"   位置：{where}", f"   原因：{i['explanation']}", f"   建议：{i['suggestion']}"]
    if d["strengths"]:
        lines.append("\n写得好的地方：\n" + "\n".join("  · " + s for s in d["strengths"]))
    if d["next_steps"]:
        lines.append("\n建议接下来：\n" + "\n".join(f"  {k}. {s}" for k, s in enumerate(d["next_steps"], 1)))
    return "\n".join(lines)
