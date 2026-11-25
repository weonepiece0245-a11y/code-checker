"""网页版：python -m codecheck.web.server  然后浏览器打开 http://127.0.0.1:8765

安全：只监听本机；只接受本应用自己发出、带有本次启动随机令牌的请求（检查 Host / Origin / 令牌）。
读写文件、检查文件夹只限用户自己选过的（见 files.py）。
"""
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, files, folder, folder_ai, store, testgen, updater
from ..ai import pick_backend
from ..core import check_code
from ..languages import detect
from ..proc import NO_WINDOW

# ---------- 全局设置 ----------

WEB_DIR = Path(__file__).parent
APP_ROOT = Path(__file__).resolve().parents[2]
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
# 每次启动随机生成的令牌：写进页面，所有 /api 请求都要带上。别的网页拿不到它，就无法调用本机服务
TOKEN = secrets.token_urlsafe(24)
MAX_RUN_SLOTS = 2                          # 同一时间最多运行 2 个检查/测试，避免把电脑占满
RUN_SLOTS = threading.BoundedSemaphore(MAX_RUN_SLOTS)
MAX_JOBS = 2                               # 同时最多进行几个 AI 审查（每个内部最多再并行 3 批）
RESTART_CODE = 10                          # 启动器看到这个退出码会重新启动
RESTART_HOOK = None                        # 桌面应用会设置自己的重启方式
WATCHDOG = False                           # 桌面应用的备用模式：页面关闭后自动退出
LAST_PING = 0.0
UPDATING = threading.Event()               # 正在下载/安装更新：这期间不再接新的检查和审查
SLOT_WAIT = 20                             # 检查排队最多等几秒，等不到就提示“太忙”
# 后台任务：分批审查可能要十几分钟，网页定时来问进度
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()                # 也保护下面的 ACTIVE，以及和 UPDATING 的配合
ACTIVE = {"ai_calls": 0}                    # 正在进行、但不占运行名额的 AI 调用（生成测试）
# Docker 能不能用：第一次有人问的时候在后台检查（docker info 可能要几秒），能用就默认用 Docker 隔离运行
DOCKER_OK: dict = {}
_docker_lock = threading.Lock()

Lang = Literal["python", "javascript"]
Sandbox = Literal["local", "docker"]
AIBackend = Literal["auto", "api", "cli", "none"]
ExplainLang = Literal["简体中文", "繁體中文", "English"]  # 固定选项：这个值会写进给 AI 的说明里
ReviewMode = Literal["review", "recheck", "continue"]

files.allow_dir(str(APP_ROOT))  # “程序位置”：可以检查程序自己


def _load_page(name: str) -> str:
    path = WEB_DIR / name
    try:
        return path.read_text(encoding="utf-8").replace("{{TOKEN}}", TOKEN)
    except OSError as e:
        raise RuntimeError(f"找不到页面文件 {path}（程序文件可能不完整，请重新下载）：{e}") from None


PAGE = _load_page("index.html")   # 一个工作区
SHELL = _load_page("shell.html")  # 外壳：标签栏 + 多个工作区

app = FastAPI(title="代码检查助手")
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")


@app.middleware("http")
async def local_only(request: Request, call_next):
    """只接受本应用自己发来的请求：防止其他网页偷偷调用本机服务（运行代码、读写文件）。"""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    origin = request.headers.get("origin")
    if host not in LOCAL_HOSTS:
        return PlainTextResponse("forbidden", status_code=403)
    if origin and origin != "null":
        o = urlparse(origin)
        if o.hostname not in LOCAL_HOSTS or (o.port and str(o.port) != str(request.url.port)):
            return PlainTextResponse("forbidden", status_code=403)
    if request.url.path.startswith("/api/") and not secrets.compare_digest(
            request.headers.get("x-token", ""), TOKEN):
        return PlainTextResponse("forbidden: bad token", status_code=403)
    return await call_next(request)


# ---------- 公共小工具 ----------

class _slot:
    """限制同时运行的检查数量；等太久就返回 429（太忙，请稍后再试）。"""
    def __enter__(self):
        if UPDATING.is_set():
            raise HTTPException(503, "程序正在更新，马上会重启，请稍等再试")
        if not RUN_SLOTS.acquire(timeout=SLOT_WAIT):
            raise HTTPException(429, "同时进行的检查太多了，请等前面的完成后再试")
        if UPDATING.is_set():  # 排队期间开始了更新
            RUN_SLOTS.release()
            raise HTTPException(503, "程序正在更新，马上会重启，请稍等再试")

    def __exit__(self, *exc):
        RUN_SLOTS.release()


class _ai_call:
    """生成测试这类要等 AI 几十秒、但不占运行名额的请求：登记一下，更新前就知道它还在进行。"""
    def __enter__(self):
        with JOBS_LOCK:  # 和 update_apply 用同一把锁：要么更新看见这个请求，要么这里看见“正在更新”
            if UPDATING.is_set():
                raise HTTPException(503, "程序正在更新，马上会重启，请稍等再试")
            ACTIVE["ai_calls"] += 1

    def __exit__(self, *exc):
        with JOBS_LOCK:
            ACTIVE["ai_calls"] -= 1


def _real_file(path: str | None) -> str | None:
    """“在原位置运行”只接受：确实存在、是代码文件、并且是用户选过的文件。"""
    if not path:
        return None
    p = Path(path)
    if p.suffix.lower() in files.ALLOWED and p.is_file() and files.is_allowed(str(p)):
        return str(p.resolve())
    return None


def _folder(path: str) -> str:
    """文件夹相关的操作只限用户用“检查文件夹”选过的文件夹（或程序自己的文件夹）。"""
    full = os.path.abspath(path)
    if not files.is_allowed(full):
        raise PermissionError("请先点“📁 检查整个项目文件夹”选择这个文件夹")
    return full


# ---------- 页面和基本信息 ----------

@app.get("/", response_class=HTMLResponse)
def index():
    return SHELL


@app.get("/workspace", response_class=HTMLResponse)
def workspace():
    return PAGE


@app.get("/health")
def health():
    """给启动程序确认“服务已经起来了”用：不需要令牌，也不包含任何敏感信息。"""
    return {"app": "codecheck", "version": __version__}


def _check_docker():
    if not shutil.which("docker"):
        DOCKER_OK["ok"] = False
        return
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                           timeout=8, creationflags=NO_WINDOW)
        DOCKER_OK["ok"] = r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        DOCKER_OK["ok"] = False


def _ensure_docker_check():
    with _docker_lock:
        if "started" not in DOCKER_OK:
            DOCKER_OK["started"] = True
            threading.Thread(target=_check_docker, daemon=True).start()


@app.get("/api/info")
def info():
    _ensure_docker_check()
    return {"ai_auto": pick_backend("auto"), "version": __version__, "root": str(APP_ROOT),
            "docker": DOCKER_OK.get("ok", False)}


@app.post("/api/ping")
def ping():
    global LAST_PING
    LAST_PING = time.time()
    return {"ok": True}


# ---------- 自动更新 ----------

@app.get("/api/update/check")
def update_check():
    return updater.check()


def _busy_reason() -> str:
    """有没有正在进行的检查或审查（更新会重启程序，会把它们打断）。"""
    with JOBS_LOCK:
        jobs = sum(1 for j in JOBS.values() if j["status"] == "running")
        calls = ACTIVE["ai_calls"]
    if jobs:
        return f"有 {jobs} 个 AI 审查正在进行"
    if calls:
        return "AI 正在生成测试"
    taken = 0
    while taken < MAX_RUN_SLOTS and RUN_SLOTS.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        RUN_SLOTS.release()
    if taken < MAX_RUN_SLOTS:
        return "有检查或测试正在运行"
    return ""


@app.post("/api/update/apply")
def update_apply():
    if UPDATING.is_set():
        return {"ok": False, "message": "已经在更新了，请稍等"}
    # 先挂上“正在更新”的牌子再检查忙不忙：这样检查完到真正安装之间，不会有新的检查或审查插进来
    UPDATING.set()
    busy = _busy_reason()
    if busy:
        UPDATING.clear()
        return {"ok": False, "message": f"{busy}，更新会重启程序把它打断。请等它完成（或取消）后再更新。"}
    try:
        info_ = updater.check()
        if not info_["has_update"]:
            UPDATING.clear()
            return {"ok": False, "message": info_["error"] or "已经是最新版本"}
        updater.apply(updater.update_source(), info_["remote"])
    except Exception as e:
        UPDATING.clear()
        return {"ok": False, "message": f"下载更新失败：{e}"}
    # 先把结果返回给网页，再退出，让启动器用新版本重新启动
    threading.Timer(1.0, RESTART_HOOK or (lambda: os._exit(RESTART_CODE))).start()
    return {"ok": True, "message": f"已更新到 {info_['remote']}，正在重启……", "version": info_["remote"]}


# ---------- 检查和测试 ----------

class CheckRequest(BaseModel):
    code: str = Field(max_length=200_000)
    language: Lang | None = None
    stdin: str = Field("", max_length=100_000)
    run: bool = True
    timeout: int = Field(5, ge=1, le=60)
    sandbox: Sandbox = "local"
    ai: AIBackend = "auto"
    explain_lang: ExplainLang = "简体中文"
    source_path: str | None = None  # 打开的真实文件：运行时像在原位置一样（工作目录也由它决定）


@app.post("/api/check")
def check(req: CheckRequest):
    with _slot():
        try:
            return check_code(req.code, None, req.language or None, req.stdin, req.run, req.timeout,
                              sandbox=req.sandbox, ai_backend=req.ai, explain_lang=req.explain_lang,
                              source_path=_real_file(req.source_path))
        except Exception as e:  # 和其他接口一样：出错时返回说明，而不是笼统的 500
            return {"error": f"检查时出错：{type(e).__name__}: {e}"}


class GenTestsRequest(BaseModel):
    code: str = Field(max_length=200_000)
    language: Lang | None = None
    source_path: str | None = None  # 打开的文件：按后缀确定语言，和检查时一致
    ai: AIBackend = "auto"
    explain_lang: ExplainLang = "简体中文"


@app.post("/api/tests/generate")
def tests_generate(req: GenTestsRequest):
    with _ai_call():
        try:
            source = _real_file(req.source_path)
            language = req.language or detect(source, req.code).key
            return testgen.generate(req.code, language, req.explain_lang, req.ai, source_path=source)
        except Exception as e:
            return {"error": str(e)}


class RunTestsRequest(BaseModel):
    code: str = Field(max_length=200_000)
    language: Lang | None = None
    tests: list[dict] = Field(max_length=30)
    timeout: int = Field(5, ge=1, le=60)
    sandbox: Sandbox = "local"
    source_path: str | None = None  # 打开的真实文件：测试也在原位置运行
    allow_risky: bool | list[int] = False  # 用户看过并同意后才运行有危险操作的测试：True = 全部，列表 = 这几个编号


@app.post("/api/tests/run")
def tests_run(req: RunTestsRequest):
    with _slot():  # 太忙时返回 429，和 /api/check 一致
        try:
            return testgen.run(req.code, req.tests, req.language or None, req.timeout, req.sandbox,
                               source_path=_real_file(req.source_path), allow_risky=req.allow_risky)
        except Exception as e:
            return {"error": str(e)}


# ---------- 文件和文件夹 ----------

class PickRequest(BaseModel):
    kind: Literal["file", "folder"] = "file"


@app.post("/api/fs/pick")
def fs_pick(req: PickRequest):
    try:
        path = files.pick(req.kind)
    except Exception as e:
        return {"error": f"打不开选择窗口：{e}"}
    return {"path": path} if path else {"cancelled": True}


class PathRequest(BaseModel):
    path: str
    exclude: list[str] | None = None  # 不传就用保存的设置


@app.post("/api/fs/read")
def fs_read(req: PathRequest):
    try:
        # 服务端先查一遍（files.read 里还会再查一次，双重保险）：
        # 只能是代码文件，并且是用户选过的文件，或用户选过的文件夹里的文件
        if not files.has_allowed_ext(req.path):
            raise ValueError("只能打开 .py / .js / .mjs / .cjs 文件")
        if not files.is_allowed(req.path):
            raise PermissionError("只能打开你用“打开文件”选过的文件，或“检查文件夹”选过的文件夹里的文件")
        return files.read(req.path)
    except Exception as e:
        return {"error": str(e)}


class SaveRequest(BaseModel):
    path: str
    content: str = Field(max_length=1_000_000)
    encoding: str = "utf-8"
    crlf: bool = False


@app.post("/api/fs/save")
def fs_save(req: SaveRequest):
    try:
        # 只能保存回这次运行期间打开过的代码文件（不能新建，也不能写别的路径）
        if not files.has_allowed_ext(req.path) or not files.was_opened(req.path):
            raise PermissionError("只能保存回之前打开过的文件")
        return files.save(req.path, req.content, req.encoding, req.crlf)
    except Exception as e:
        return {"error": str(e)}


def _exclude(req) -> list[str]:
    if req.exclude is None:
        return store.get("exclude", os.path.abspath(req.path), [])
    return req.exclude


class ExcludeRequest(BaseModel):
    path: str
    exclude: list[str]


@app.post("/api/fs/exclude")
def fs_exclude(req: ExcludeRequest):
    try:
        store.put("exclude", _folder(req.path), sorted(set(req.exclude)))
        return {"ok": True}
    except Exception as e:
        return {"error": f"保存排除设置失败：{e}"}


@app.post("/api/fs/scan")
def fs_scan(req: PathRequest):
    try:
        path = _folder(req.path)
        r = folder.scan(path, _exclude(req))
        last = folder_ai.last_review(path)
        r["last_review"] = ({"time": last["time"], "issues": len(last["issues"]),
                             "pending": len(last.get("pending", []))} if last else None)
        return r
    except Exception as e:
        return {"error": str(e)}


# ---------- AI 审查整个文件夹 ----------

class AIPlanRequest(BaseModel):
    path: str
    exclude: list[str] | None = None
    mode: ReviewMode = "review"


@app.post("/api/fs/ai_plan")
def fs_ai_plan(req: AIPlanRequest):
    """开始前预览：要审查多少文件、分几批、大约多少字符。"""
    try:
        return folder_ai.preview(_folder(req.path), req.mode, _exclude(req))
    except Exception as e:
        return {"error": str(e)}


class AIReviewRequest(BaseModel):
    path: str
    exclude: list[str] | None = None
    ai: AIBackend = "auto"
    explain_lang: ExplainLang = "简体中文"
    mode: ReviewMode = "review"


def _run_review(req: AIReviewRequest, progress=None, cancel=None) -> dict:
    path = _folder(req.path)
    # 选了“不用 AI”时，审查会给出提示，而不是偷偷改用 AI
    if req.mode == "recheck":
        return folder_ai.recheck(path, req.explain_lang, req.ai, _exclude(req), progress, cancel)
    return folder_ai.review(path, req.explain_lang, req.ai, _exclude(req), progress, cancel, mode=req.mode)


# AI 审查只通过后台任务进行：能看进度、能取消，更新前也知道它还在进行
@app.post("/api/fs/ai_job")
def fs_ai_job(req: AIReviewRequest):
    try:
        _folder(req.path)
    except PermissionError as e:
        return {"error": str(e)}
    job = {"status": "running", "done": 0, "total": 0, "result": None, "error": None,
           "cancel": threading.Event(), "started": time.time()}
    with JOBS_LOCK:  # 数数量和登记新任务放在同一把锁里：同时点几次也不会超过上限
        if UPDATING.is_set():  # 和更新共用这把锁：要么更新看见这个任务，要么这里看见更新
            return {"error": "程序正在更新，马上会重启，请稍等再试"}
        running = sum(1 for j in JOBS.values() if j["status"] == "running")
        if running >= MAX_JOBS:
            return {"error": f"已经有 {running} 个 AI 审查正在进行，请等它们完成或取消后再开始新的。"}
        finished = [k for k, v in JOBS.items() if v["status"] != "running"]
        for k in finished[:-20]:  # 只保留最近 20 个已结束的任务
            JOBS.pop(k, None)
        job_id = secrets.token_hex(8)
        JOBS[job_id] = job

    def progress(done: int, total: int):
        job["done"], job["total"] = done, total

    def work():
        try:
            result = _run_review(req, progress, job["cancel"])
            if job["cancel"].is_set():
                # 用户取消了：已经审完的部分已保存（可以“继续审查”），但这次不算完成
                job["error"] = "已取消。已经审完的部分已保存，可以点“继续审查”接着审。"
                job["status"] = "cancelled"
            else:
                job["result"] = result
                job["status"] = "done"
        except Exception as e:
            job["error"] = str(e)
            job["status"] = "cancelled" if job["cancel"].is_set() else "error"

    threading.Thread(target=work, daemon=True).start()
    return {"job": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return {"status": "missing", "error": "找不到这个任务（程序可能重启过）"}
    out = {k: job[k] for k in ("status", "done", "total", "error")}
    out["elapsed"] = int(time.time() - job["started"])
    if job["status"] == "done":
        out["result"] = job["result"]
    return out


@app.post("/api/jobs/{job_id}/cancel")
def job_cancel(job_id: str):
    job = JOBS.get(job_id)
    if job:
        job["cancel"].set()
    return {"ok": True}


# ---------- 直接运行网页版 ----------

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if sys.platform != "win32":  # 和 uvicorn 一致，忽略刚关闭连接的 TIME_WAIT 状态
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _is_ours(port: int) -> bool:
    """这个端口上跑的是不是本程序（之前打开的窗口没关）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.loads(r.read()).get("app") == "codecheck"
    except Exception:
        return False


def main():
    import uvicorn

    try:
        port = int(os.environ.get("PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        print(f"环境变量 PORT 的值不对（{os.environ.get('PORT')!r}），改用默认端口 8765")
        port = 8765
    # 更新后重启时，旧进程刚退出，端口可能还要几秒才释放，等一等
    wait = 15 if os.environ.get("CODECHECK_NO_BROWSER") else 3
    for _ in range(wait * 2):
        if _port_free(port) or _is_ours(port):
            break
        time.sleep(0.5)
    if not _port_free(port):
        if _is_ours(port):
            print(f"代码检查助手已经在运行了（另一个窗口里）：http://127.0.0.1:{port}")
            print("如果你刚更新了程序，请先关掉那个旧窗口，再重新启动，新版本才会生效。")
            webbrowser.open(f"http://127.0.0.1:{port}")
            return
        free = next((p for p in range(port + 1, port + 50) if _port_free(p)), None)
        if free is None:
            print(f"端口 {port} 到 {port + 49} 都被其他程序占用了，没法启动。"
                  "可以关掉一些程序再试，或者设置环境变量 PORT 换一个端口。")
            sys.exit(1)
        print(f"端口 {port} 被其他程序占用，改用 {free}。")
        port = free
    url = f"http://127.0.0.1:{port}"
    print(f"代码检查助手已启动：{url}")
    print("用完后关闭这个窗口即可停止。")
    if not os.environ.get("CODECHECK_NO_BROWSER"):  # 更新后重启时不再开新标签页
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()  # 自动打开浏览器
    # 只监听本机：这个服务会运行代码，不要暴露到公网
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
