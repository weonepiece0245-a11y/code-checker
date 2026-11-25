"""AI 审查整个文件夹：把项目里的代码（加上静态检查结果）交给 AI，找逻辑错误、安全隐患等。

项目太大时自动分批：每批不超过 CHAR_BUDGET 个字符，同一文件夹的文件尽量放在同一批，
最多同时审查 MAX_PARALLEL 批，最后把所有问题合并，再让 AI 写整体概况。
每次审查都会记下每个文件的“指纹”（内容的哈希）：复查时只看改动过的文件和有问题的文件。
额度用完等原因没审完的文件会记下来，可以之后“继续审查”。
"""
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import ai, folder, store, textio

CHAR_BUDGET = int(os.environ.get("CODECHECK_BATCH_CHARS", "180000"))  # 每批最多发送多少字符（大约 6 万~9 万 token）
MAX_PARALLEL = 3       # 最多同时审查几批
MAX_LIST = 1500        # 每批附带的“项目全部文件列表”最多列多少个

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

请审查这些代码，重点找静态检查发现不了的问题，按重要程度：
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


BATCH_NOTE = """这个项目比较大，分成了 {total} 批审查，这是第 {index} 批（共 {count} 个文件）。
下面先给出项目里全部文件的列表，帮助你理解整体结构；但只报告【这一批】文件里的问题。
如果怀疑问题和其他文件有关，可以在 explanation 里说明。"""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "strengths", "next_steps"],
    "additionalProperties": False,
}

SUMMARY_SYSTEM = """你是一位经验丰富的代码审查员。一个较大的项目被分成多批审查，下面是每一批的概况和发现的问题清单。
请据此写出整个项目的总结：
- overview：两三句话说明这个项目是做什么的、整体质量如何、最主要的风险在哪里。
- strengths：1~3 个写得好的地方。
- next_steps：3 条最值得先做的改进（优先处理严重问题，可以把相似的问题合并成一条）。
全部用{explain_lang}回答。读者可能是编程初学者，说话直接但友善。
"""


# ---------- 分批 ----------

def _file_block(f: dict, text: str) -> str:
    """一个文件真正要发给 AI 的内容：静态检查结果 + 带行号的代码。"""
    numbered = textio.number_lines(text)
    lint = "\n".join(f"  第 {i['line']} 行 [{i['code']}] {i['message']}" for i in f["issues"]) or "  无"
    return f"===== 文件：{f['rel']} =====\n静态检查发现：\n{lint}\n代码：\n{numbered}"


def _estimate(f: dict) -> int:
    """不读文件，估算发送这个文件要多少字符：文件字节数（≥ 字符数）+ 每行的行号前缀 + 检查结果。"""
    try:
        size = os.path.getsize(f["path"])
    except OSError:
        return 0
    lint = sum(len(i["message"]) + 30 for i in f["issues"]) or 4
    return size + (f["lines"] + 1) * 7 + lint + len(f["rel"]) + 40


def _norm(rel: str) -> str:
    rel = rel.replace("\\", "/")
    while rel.startswith("./"):  # 只去掉“./”前缀；不能用 lstrip，它会把 .eslintrc.js 的点也去掉
        rel = rel[2:]
    return rel.lower()


def make_batches(files: list[dict]) -> list[list[dict]]:
    """按路径排序后依次装批（同一文件夹的文件挨在一起），每批不超过 CHAR_BUDGET。
    单个文件本身就超过上限时，单独成一批（AI 能读的总量远大于这个上限）。"""
    batches, cur, used = [], [], 0
    for f in sorted(files, key=lambda f: f["rel"].replace("\\", "/").lower()):
        est = _estimate(f)
        if cur and used + est > CHAR_BUDGET:
            batches.append(cur)
            cur, used = [], 0
        cur.append(f)
        used += est
    if cur:
        batches.append(cur)
    return batches


def _fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _read_batch(batch: list[dict]) -> tuple[list[str], list[str], dict]:
    """读取一批文件：返回 (要发送的内容, 读取失败的文件, {文件: 指纹})。"""
    blocks, unreadable, prints = [], [], {}
    for f in batch:
        try:
            text = textio.read(f["path"]).text
        except OSError:
            unreadable.append(f["rel"])
            continue
        blocks.append(_file_block(f, text))
        prints[_norm(f["rel"])] = _fingerprint(text)
    return blocks, unreadable, prints


def _project_list(scan: dict) -> str:
    rels = sorted(f["rel"].replace("\\", "/") for f in scan["files"])
    more = f"\n……还有 {len(rels) - MAX_LIST} 个文件" if len(rels) > MAX_LIST else ""
    return "项目全部文件：\n" + "\n".join(rels[:MAX_LIST]) + more


def _current_prints(scan: dict) -> dict:
    out = {}
    for f in scan["files"]:
        try:
            out[_norm(f["rel"])] = _fingerprint(textio.read(f["path"]).text)
        except OSError:
            pass
    return out


# ---------- 保存 / 读取上次的结果 ----------

ORDER = {"high": 0, "medium": 1, "low": 2}
ISSUE_KEYS = ("file", "line", "severity", "category", "title", "explanation", "suggestion")


def _save(folder_path: str, issues: list[dict], overview: str, prints: dict, pending: list[str]):
    keep = [{k: i[k] for k in ISSUE_KEYS} for i in issues]
    store.put("review", folder_path, {"time": store.now(), "overview": overview, "issues": keep,
                                      "prints": prints, "pending": sorted(set(pending))})


def last_review(path: str) -> dict | None:
    return store.get("review", os.path.abspath(path))


def _attach_paths(issues: list[dict], scan: dict):
    """把 AI 说的相对路径对应回真实文件，方便界面里点击打开。"""
    by_rel = {_norm(f["rel"]): f["path"] for f in scan["files"]}
    for issue in issues:
        issue["path"] = by_rel.get(_norm(issue["file"]))


# ---------- 开始前预览 ----------

def preview(path: str, mode: str = "review", exclude: list[str] | None = None) -> dict:
    """开始前告诉用户：要审查多少文件、分几批、大约多少字符。不读文件内容（复查除外）。"""
    scan = folder.scan(path, exclude, compare=False)
    last = last_review(path)
    files = _select(scan, mode, last)
    batches = make_batches(files)
    out = {"mode": mode, "total": scan["total"], "files": len(files), "batches": len(batches),
           "chars": sum(_estimate(f) for f in files),
           "last_review": ({"time": last["time"], "issues": len(last["issues"]),
                            "pending": len(last.get("pending", []))} if last else None)}
    if mode == "recheck" and last:
        out["unchanged"] = scan["total"] - len(files)
    return out


def _select(scan: dict, mode: str, last: dict | None) -> list[dict]:
    """这次要发送哪些文件。"""
    files = scan["files"]
    if mode == "continue":
        pending = {_norm(r) for r in (last or {}).get("pending", [])}
        return [f for f in files if _norm(f["rel"]) in pending]
    if mode == "recheck" and last:
        old = last.get("prints") or {}
        if not old:  # 旧版本的审查记录没有指纹：只能全部重审
            return files
        now = _current_prints(scan)
        # 只发改动过的文件、新文件、上次没审完的文件。
        # 没改动的文件里的问题一定还在，直接判为“未解决”，不用再花额度
        pending = {_norm(r) for r in last.get("pending", [])}
        return [f for f in files
                if _norm(f["rel"]) in pending or old.get(_norm(f["rel"])) != now.get(_norm(f["rel"]))]
    return files


# ---------- 分批运行 ----------

def _run_batches(batches, make_prompt, schema, explain_lang, backend, progress, cancel):
    """并行跑所有批次。返回 ([(批次序号, AI 结果, 指纹, 读取失败)], [(批次序号, 出错原因)], 用到的后端)。"""
    done, failed, used_backend = [], [], None
    lock = threading.Lock()
    total = len(batches)

    def one(index: int, batch: list[dict]):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("已取消")
        blocks, unreadable, prints = _read_batch(batch)
        if not blocks:
            return None, prints, unreadable
        used, data = ai.structured(make_prompt[0].format(explain_lang=explain_lang),
                                   make_prompt[1](index, batch, blocks), schema, backend, timeout=900,
                                   cancel=cancel)
        return (used, data), prints, unreadable

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, max(1, total))) as pool:
        futures = {pool.submit(one, i, b): i for i, b in enumerate(batches)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                result, prints, unreadable = fut.result()
                with lock:
                    if result:
                        used_backend = used_backend or result[0]
                    done.append((i, result[1] if result else None, prints, unreadable))
            except Exception as e:
                with lock:
                    failed.append((i, str(e)))
            if progress:
                progress(len(done) + len(failed), total)
    done.sort()
    return done, failed, used_backend


def _summarize(overviews: list[str], issues: list[dict], explain_lang: str, backend: str) -> dict:
    """多批审查后，让 AI 根据各批的概况和问题清单写整体总结；失败时退回简单拼接。"""
    lines = [f"第 {n} 批概况：{o}" for n, o in enumerate(overviews, 1)]
    lines.append("\n全部问题：")
    lines += [f"- [{i['severity']}/{i['category']}] {i['file']}：{i['title']}" for i in issues[:300]]
    try:
        _, data = ai.structured(SUMMARY_SYSTEM.format(explain_lang=explain_lang), "\n".join(lines),
                                SUMMARY_SCHEMA, backend, timeout=600)
        return data
    except Exception:
        return {"overview": " ".join(overviews[:3]), "strengths": [],
                "next_steps": [i["title"] for i in issues[:3]]}


# ---------- 审查 ----------

def review(path: str, explain_lang: str = "简体中文", backend: str = "auto",
           exclude: list[str] | None = None, progress=None, cancel=None, mode: str = "review") -> dict:
    """审查整个文件夹（mode="review"），或继续审查上次没审完的文件（mode="continue"）。"""
    ai.require_ai(backend)
    scan = folder.scan(path, exclude, compare=False)
    last = last_review(path) if mode == "continue" else None
    files = _select(scan, mode, last)
    if not files:
        raise ValueError("没有需要审查的文件" if mode == "continue" else "这个文件夹里没有可以审查的代码文件")
    batches = make_batches(files)
    total = len(batches)
    project_list = _project_list(scan) if total > 1 else ""

    def prompt(index, batch, blocks):
        head = [f"项目文件夹：{os.path.basename(scan['folder'])}，共 {scan['total']} 个代码文件。"]
        if total > 1:
            head += [BATCH_NOTE.format(total=total, index=index + 1, count=len(batch)), project_list]
        return "\n\n".join(head + blocks)

    done, failed, used = _run_batches(batches, (SYSTEM, prompt), SCHEMA, explain_lang, backend, progress, cancel)
    if not done or all(d[1] is None for d in done):
        reason = failed[0][1] if failed else "没有读到任何文件"
        raise RuntimeError(f"AI 审查失败：{reason}")

    issues, overviews, strengths, next_steps, prints, unreadable = [], [], [], [], {}, []
    for _, data, pr, unr in done:
        prints.update(pr)
        unreadable += unr
        if data:
            issues += data["issues"]
            overviews.append(data["overview"])
            strengths += data["strengths"]
            next_steps += data["next_steps"]
    issues.sort(key=lambda i: ORDER.get(i["severity"], 3))
    _attach_paths(issues, scan)

    if total > 1:
        summary = _summarize(overviews, issues, explain_lang, used or backend)
    else:
        summary = {"overview": overviews[0] if overviews else "", "strengths": strengths, "next_steps": next_steps}

    failed_files = [f["rel"] for i, _ in failed for f in batches[i]]
    if mode == "continue" and last:
        # 接着上次：把这次的结果并进上次的报告
        old_issues = [i for i in last["issues"]]
        _attach_paths(old_issues, scan)
        issues = sorted(old_issues + issues, key=lambda i: ORDER.get(i["severity"], 3))
        prints = {**(last.get("prints") or {}), **prints}
        summary["overview"] = last.get("overview") or summary["overview"]
    _save(scan["folder"], issues, summary["overview"], prints, failed_files + unreadable)

    reviewed = [f["rel"] for i, _, _, _ in done for f in batches[i]]
    return {**summary, "issues": issues, "folder": scan["folder"], "reviewed": reviewed,
            "batches": total, "failed_batches": [{"index": i + 1, "error": e} for i, e in sorted(failed)],
            "pending": sorted(set(failed_files + unreadable)),
            "pending_groups": folder.group_by_dir(failed_files + unreadable),
            "skipped": [], "skipped_groups": [], "ai_backend": used, "mode": mode}


# ---------- AI 复查：上次的问题解决了吗 ----------

RECHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "previous": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "status": {"type": "string", "enum": ["fixed", "partial", "not_fixed", "unclear"]},
                    "note": {"type": "string"},
                    "line": {"type": "integer"},
                },
                "required": ["index", "status", "note", "line"],
                "additionalProperties": False,
            },
        },
        "new_issues": SCHEMA["properties"]["issues"],
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "previous", "new_issues", "next_steps"],
    "additionalProperties": False,
}

RECHECK_SYSTEM = """你是一位严谨、友善的代码审查员，读者可能是编程初学者。
之前你审查过这个项目，列出了一些问题。用户说已经去修改了。现在给你：上次的问题清单（带编号），
以及项目【现在】的代码（每行前有行号）和静态检查结果。

请做两件事：
1. 逐条判断上次的每个问题现在的状态，previous 里每个编号都要有且只有一条：
   - fixed：问题已经彻底解决
   - partial：改了一部分，但还没完全解决（note 里说清楚还差什么、怎么补）
   - not_fixed：还没有解决（note 里说明现在代码里问题还在哪里）
   - unclear：相关的文件这次没有提供，或者代码改动太大无法判断（note 里说明原因）
   fixed 的 note 用一句话说明是怎么解决的，让用户确认没改错。
   line 填现在代码里对应的行号（已解决或找不到就填 0）。
   请认真核对现在的代码，不要因为用户说改了就判断为已解决。
2. new_issues：检查修改有没有引入新的问题，或者有没有上次漏掉的重要问题（最多 10 条，宁缺毋滥；
   不要和上次清单里的问题重复）。格式和要求与普通审查相同，file 必须和给出的文件路径完全一致。

overview 用两三句话总结这次修改的整体效果；next_steps 列出接下来最值得做的 1~3 件事。
全部用{explain_lang}回答。
"""


UNCHANGED_NOTE = "这个文件和上次审查时完全一样，没有改动，所以问题还在。"


def recheck(path: str, explain_lang: str = "简体中文", backend: str = "auto",
            exclude: list[str] | None = None, progress=None, cancel=None) -> dict:
    """只重新审查改动过的文件（以及新文件、上次没审完的文件）；没改动的文件里的问题直接判为“未解决”。"""
    ai.require_ai(backend)
    last = last_review(path)
    if not last or not last.get("issues"):
        raise ValueError("这个文件夹还没有做过 AI 审查，请先点“AI 审查整个文件夹”")
    scan = folder.scan(path, exclude, compare=False)
    files = _select(scan, "recheck", last)
    now_prints = _current_prints(scan)
    old_prints = last.get("prints") or {}
    existing = {_norm(f["rel"]) for f in scan["files"]}

    previous = [dict(i, k=n) for n, i in enumerate(last["issues"], 1)]
    status: dict[int, dict] = {}
    for i in previous:
        rel = _norm(i["file"])
        if rel not in existing:
            status[i["k"]] = {"status": "unclear", "note": "这个文件已经不在了（被删除、改名或被排除）。", "line": 0}
        elif old_prints and old_prints.get(rel) == now_prints.get(rel):
            status[i["k"]] = {"status": "not_fixed", "note": UNCHANGED_NOTE, "line": i["line"]}

    batches = make_batches(files) if files else []
    by_batch_prev = []
    for batch in batches:
        rels = {_norm(f["rel"]) for f in batch}
        by_batch_prev.append([i for i in previous if _norm(i["file"]) in rels and i["k"] not in status])
    total = len(batches)
    project_list = _project_list(scan) if total > 1 else ""

    def prompt(index, batch, blocks):
        prev = by_batch_prev[index]
        prev_lines = [f"[{i['k']}] （{i['severity']}/{i['category']}）{i['title']}\n    位置（上次）：{i['file']}"
                      + (f" 第 {i['line']} 行" if i["line"] else "")
                      + f"\n    说明：{i['explanation']}\n    当时的建议：{i['suggestion']}" for i in prev]
        head = [f"===== 上次审查（{last['time']}）在这批文件里发现的问题 =====\n" + ("\n".join(prev_lines) or "（这批文件上次没有发现问题）")]
        if total > 1:
            head += [BATCH_NOTE.format(total=total, index=index + 1, count=len(batch)), project_list]
        return "\n\n".join(head + ["===== 这批文件现在的代码 =====", *blocks])

    done, failed, used = ([], [], None)
    if batches:
        done, failed, used = _run_batches(batches, (RECHECK_SYSTEM, prompt), RECHECK_SCHEMA,
                                          explain_lang, backend, progress, cancel)
    elif progress:
        progress(1, 1)
    if batches and not done:
        raise RuntimeError(f"AI 复查失败：{failed[0][1] if failed else '未知原因'}")

    new_issues, overviews, next_steps, unreadable = [], [], [], []
    prints = dict(old_prints)
    for idx, data, pr, unr in done:
        prints.update(pr)
        unreadable += unr
        if not data:
            continue
        allowed = {i["k"] for i in by_batch_prev[idx]}
        for x in data["previous"]:
            if x["index"] in allowed:
                status[x["index"]] = x
        new_issues += data["new_issues"]
        overviews.append(data["overview"])
        next_steps += data["next_steps"]
    for i, _ in failed:  # 这批没复查成：里面的问题记为“无法判断”
        for p in by_batch_prev[i]:
            status.setdefault(p["k"], {"status": "unclear", "note": "这一批复查失败了，这次没能判断。", "line": 0})

    result_prev = []
    for i in previous:
        st = status.get(i["k"], {"status": "unclear", "note": "AI 没有给出判断", "line": 0})
        if st["status"] == "fixed":
            line = i["line"]                      # 已解决：保留上次的位置
        else:
            line = st.get("line") or i["line"]    # 没解决：优先用 AI 给出的现在的行号
        result_prev.append({**{k: i[k] for k in ISSUE_KEYS}, "status": st["status"], "note": st["note"], "line": line})
    _attach_paths(result_prev, scan)
    _attach_paths(new_issues, scan)
    new_issues.sort(key=lambda i: ORDER.get(i["severity"], 3))

    failed_files = [f["rel"] for i, _ in failed for f in batches[i]]
    still = [i for i in result_prev if i["status"] in ("partial", "not_fixed", "unclear")]
    overview = " ".join(overviews[:3]) if overviews else "这次没有文件需要重新审查：上次有问题的文件都没有改动。"
    # 还没审完的文件：只记这次仍然失败或读不到的。上次没审完的文件这次已经被选进来审过了，
    # 不能再原样写回去（否则每次复查都会重复发送，界面也一直显示“没审完”）
    still_pending = failed_files + unreadable
    _save(scan["folder"], still + new_issues, overview, {**prints, **now_prints} if not failed else prints,
          still_pending)

    counts = {k: sum(1 for i in result_prev if i["status"] == k) for k in ("fixed", "partial", "not_fixed", "unclear")}
    return {"mode": "recheck", "last_time": last["time"], "overview": overview, "previous": result_prev,
            "counts": counts, "new_issues": new_issues, "next_steps": next_steps[:5],
            "folder": scan["folder"], "reviewed": [f["rel"] for f in files], "batches": total,
            "unchanged": scan["total"] - len(files),
            "failed_batches": [{"index": i + 1, "error": e} for i, e in sorted(failed)],
            "pending": sorted(set(still_pending)), "pending_groups": folder.group_by_dir(still_pending),
            "skipped": [], "skipped_groups": [], "ai_backend": used}


STATUS = {"fixed": "✅ 已解决", "partial": "⚠️ 部分解决", "not_fixed": "❌ 未解决", "unclear": "❔ 无法判断"}


def format_recheck(d: dict) -> str:
    c = d["counts"]
    lines = [f"和上次 AI 审查（{d['last_time']}）相比（重新审查了 {len(d['reviewed'])} 个改动过的文件，"
             f"{d.get('unchanged', 0)} 个没改动的文件跳过）：", d["overview"],
             f"上次 {len(d['previous'])} 个问题：已解决 {c['fixed']} · 部分解决 {c['partial']} · "
             f"未解决 {c['not_fixed']} · 无法判断 {c['unclear']}"]
    for n, i in enumerate(d["previous"], 1):
        lines.append(f"\n{n}. {STATUS[i['status']]}  {i['title']}（{i['file']}）\n   {i['note']}")
    if d["new_issues"]:
        lines.append("\n新发现的问题：")
        for n, i in enumerate(d["new_issues"], 1):
            where = i["file"] + (f" 第 {i['line']} 行" if i["line"] else "")
            lines += [f"{n}. {SEV.get(i['severity'], '')} {i['title']}（{where}）",
                      f"   原因：{i['explanation']}", f"   建议：{i['suggestion']}"]
    if d["next_steps"]:
        lines.append("\n建议接下来：\n" + "\n".join(f"  {k}. {s}" for k, s in enumerate(d["next_steps"], 1)))
    return "\n".join(lines)


SEV = {"high": "🔴 严重", "medium": "🟠 中等", "low": "🟡 轻微"}
CAT = {"bug": "逻辑错误", "security": "安全", "performance": "性能", "error_handling": "错误处理",
       "structure": "结构", "readability": "可读性"}


def format_report(d: dict) -> str:
    lines = ["项目概况：" + d["overview"], f"审查了 {len(d['reviewed'])} 个文件"
             + (f"（分 {d['batches']} 批）" if d.get("batches", 1) > 1 else "")
             + (f"，{len(d['pending'])} 个没审完（可以之后“继续审查”）" if d.get("pending") else "")]
    for n, i in enumerate(d["issues"], 1):
        where = i["file"] + (f" 第 {i['line']} 行" if i["line"] else "")
        lines += [f"\n{n}. {SEV.get(i['severity'], '')} [{CAT.get(i['category'], '')}] {i['title']}",
                  f"   位置：{where}", f"   原因：{i['explanation']}", f"   建议：{i['suggestion']}"]
    if d["strengths"]:
        lines.append("\n写得好的地方：\n" + "\n".join("  · " + s for s in d["strengths"]))
    if d["next_steps"]:
        lines.append("\n建议接下来：\n" + "\n".join(f"  {k}. {s}" for k, s in enumerate(d["next_steps"], 1)))
    return "\n".join(lines)
