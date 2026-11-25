"""网页版：python -m codecheck.web.server  然后浏览器打开 http://127.0.0.1:8765"""
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, files, folder, folder_ai, store, testgen, updater
from ..ai import pick_backend
from ..core import check_code

app = FastAPI(title="代码检查助手")
# 每次启动随机生成的令牌：写进页面，所有 /api 请求都要带上。别的网页拿不到它，就无法调用本机服务
TOKEN = secrets.token_urlsafe(24)
_WEB = Path(__file__).parent
PAGE = (_WEB / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", TOKEN)   # 一个工作区
SHELL = (_WEB / "shell.html").read_text(encoding="utf-8").replace("{{TOKEN}}", TOKEN)  # 外壳：标签栏 + 多个工作区
# 同一时间最多运行 2 个检查/测试，避免把电脑占满
RUN_SLOTS = threading.BoundedSemaphore(2)

Lang = Literal["python", "javascript"]
Sandbox = Literal["local", "docker"]
AIBackend = Literal["auto", "api", "cli", "none"]
ExplainLang = Literal["简体中文", "繁體中文", "English"]  # 固定选项：这个值会写进给 AI 的说明里
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    """只接受本应用自己发来的请求：防止其他网页偷偷调用本机服务（运行代码、读写文件）。"""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    origin = request.headers.get("origin")
    if host not in LOCAL_HOSTS:
        return PlainTextResponse("forbidden", status_code=403)
    if origin and origin != "null":
        from urllib.parse import urlparse
        o = urlparse(origin)
        if o.hostname not in LOCAL_HOSTS or (o.port and str(o.port) != str(request.url.port)):
            return PlainTextResponse("forbidden", status_code=403)
    if request.url.path.startswith("/api/") and not secrets.compare_digest(
            request.headers.get("x-token", ""), TOKEN):
        return PlainTextResponse("forbidden: bad token", status_code=403)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class CheckRequest(BaseModel):
    code: str = Field(max_length=200_000)
    language: Lang | None = None
    stdin: str = Field("", max_length=100_000)
    run: bool = True
    timeout: int = Field(5, ge=1, le=60)
    sandbox: Sandbox = "local"
    ai: AIBackend = "auto"
    explain_lang: ExplainLang = "简体中文"
    workdir: str | None = None
    source_path: str | None = None  # 打开的真实文件：运行时像在原位置一样


def _real_file(path: str | None) -> str | None:
    """只接受确实存在的 .py / .js 文件作为“原位置”。"""
    if not path:
        return None
    p = Path(path)
    return str(p.resolve()) if p.suffix.lower() in files.ALLOWED and p.is_file() else None


class _slot:
    """限制同时运行的检查数量；等太久就提示稍后再试。"""
    def __enter__(self):
        if not RUN_SLOTS.acquire(timeout=120):
            from fastapi import HTTPException
            raise HTTPException(429, "同时进行的检查太多了，请稍后再试")

    def __exit__(self, *exc):
        RUN_SLOTS.release()


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


@app.get("/api/info")
def info():
    return {"ai_auto": pick_backend("auto"), "version": __version__, "root": str(Path(__file__).resolve().parents[2]),
            "docker": DOCKER_OK.get("ok", False)}


# 启动时在后台看一下 Docker 能不能用（docker info 可能要几秒），能用就默认用 Docker 隔离运行
DOCKER_OK: dict = {}


def _check_docker():
    import shutil
    import subprocess

    from ..proc import NO_WINDOW
    if not shutil.which("docker"):
        DOCKER_OK["ok"] = False
        return
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                           timeout=8, creationflags=NO_WINDOW)
        DOCKER_OK["ok"] = r.returncode == 0 and bool(r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        DOCKER_OK["ok"] = False


threading.Thread(target=_check_docker, daemon=True).start()


@app.get("/api/update/check")
def update_check():
    return updater.check()


RESTART_CODE = 10  # 启动器看到这个退出码会重新启动
RESTART_HOOK = None  # 桌面应用会设置自己的重启方式
WATCHDOG = False  # 桌面应用的备用模式：页面关闭后自动退出
LAST_PING = 0.0


@app.post("/api/ping")
def ping():
    global LAST_PING
    LAST_PING = time.time()
    return {"ok": True}


@app.post("/api/update/apply")
def update_apply():
    import threading
    info = updater.check()
    if not info["has_update"]:
        return {"ok": False, "message": info["error"] or "已经是最新版本"}
    try:
        updater.apply(updater.update_source(), info["remote"])
    except Exception as e:
        return {"ok": False, "message": f"下载更新失败：{e}"}
    # 先把结果返回给网页，再退出，让启动器用新版本重新启动
    threading.Timer(1.0, RESTART_HOOK or (lambda: os._exit(RESTART_CODE))).start()
    return {"ok": True, "message": f"已更新到 {info['remote']}，正在重启……", "version": info["remote"]}


class GenTestsRequest(BaseModel):
    code: str = Field(max_length=200_000)
    language: Lang | None = None
    source_path: str | None = None  # 打开的文件：按后缀确定语言，和检查时一致
    ai: AIBackend = "auto"
    explain_lang: ExplainLang = "简体中文"


@app.post("/api/tests/generate")
def tests_generate(req: GenTestsRequest):
    try:
        from ..languages import detect
        language = req.language or detect(_real_file(req.source_path), req.code).key
        return testgen.generate(req.code, language, req.explain_lang, req.ai,
                                source_path=_real_file(req.source_path))
    except Exception as e:
        return {"error": str(e)}


class RunTestsRequest(BaseModel):
    code: str = Field(max_length=200_000)
    language: Lang | None = None
    tests: list[dict] = Field(max_length=30)
    timeout: int = Field(5, ge=1, le=60)
    sandbox: Sandbox = "local"
    source_path: str | None = None  # 打开的真实文件：测试也在原位置运行


@app.post("/api/tests/run")
def tests_run(req: RunTestsRequest):
    try:
        with _slot():
            return testgen.run(req.code, req.tests, req.language or None, req.timeout, req.sandbox,
                               source_path=_real_file(req.source_path))
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/check")
def check(req: CheckRequest):
    with _slot():
        try:
            return check_code(req.code, None, req.language or None, req.stdin, req.run, req.timeout,
                              sandbox=req.sandbox, ai_backend=req.ai, explain_lang=req.explain_lang,
                              workdir=req.workdir, source_path=_real_file(req.source_path))
        except Exception as e:  # 和其他接口一样：出错时返回说明，而不是笼统的 500
            return {"error": f"检查时出错：{type(e).__name__}: {e}"}


# ---------- 文件和文件夹 ----------

class PickRequest(BaseModel):
    kind: str = "file"  # file / folder


@app.post("/api/fs/pick")
def fs_pick(req: PickRequest):
    try:
        path = files.pick("folder" if req.kind == "folder" else "file")
    except Exception as e:
        return {"error": f"打不开选择窗口：{e}"}
    return {"path": path} if path else {"cancelled": True}


class PathRequest(BaseModel):
    path: str
    exclude: list[str] | None = None  # 不传就用保存的设置


@app.post("/api/fs/read")
def fs_read(req: PathRequest):
    try:
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
    store.put("exclude", os.path.abspath(req.path), sorted(set(req.exclude)))
    return {"ok": True}


ReviewMode = Literal["review", "recheck", "continue"]


class AIPlanRequest(BaseModel):
    path: str
    exclude: list[str] | None = None
    mode: ReviewMode = "review"


@app.post("/api/fs/ai_plan")
def fs_ai_plan(req: AIPlanRequest):
    """开始前预览：要审查多少文件、分几批、大约多少字符。"""
    try:
        return folder_ai.preview(req.path, req.mode, _exclude(req))
    except Exception as e:
        return {"error": str(e)}


class AIReviewRequest(BaseModel):
    path: str
    exclude: list[str] | None = None
    ai: AIBackend = "auto"
    explain_lang: ExplainLang = "简体中文"
    mode: ReviewMode = "review"


def _run_review(req: "AIReviewRequest", progress=None, cancel=None) -> dict:
    ai_backend = req.ai  # 选了“不用 AI”时，审查会给出提示，而不是偷偷改用 AI
    if req.mode == "recheck":
        return folder_ai.recheck(req.path, req.explain_lang, ai_backend, _exclude(req), progress, cancel)
    return folder_ai.review(req.path, req.explain_lang, ai_backend, _exclude(req), progress, cancel, mode=req.mode)


@app.post("/api/fs/ai_review")
def fs_ai_review(req: AIReviewRequest):
    try:
        return _run_review(req)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/fs/ai_recheck")
def fs_ai_recheck(req: AIReviewRequest):
    req.mode = "recheck"
    return fs_ai_review(req)


# ---------- 后台任务：分批审查可能要十几分钟，网页定时来问进度 ----------

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_JOBS = 2  # 同时最多进行几个 AI 审查（每个内部最多再并行 3 批）


@app.post("/api/fs/ai_job")
def fs_ai_job(req: AIReviewRequest):
    with JOBS_LOCK:
        running = sum(1 for j in JOBS.values() if j["status"] == "running")
    if running >= MAX_JOBS:
        return {"error": f"已经有 {running} 个 AI 审查正在进行，请等它们完成或取消后再开始新的。"}
    job_id = secrets.token_hex(8)
    job = {"status": "running", "done": 0, "total": 0, "result": None, "error": None,
           "cancel": threading.Event(), "started": time.time()}
    with JOBS_LOCK:
        finished = [k for k, v in JOBS.items() if v["status"] != "running"]
        for k in finished[:-20]:  # 只保留最近 20 个已结束的任务
            JOBS.pop(k, None)
        JOBS[job_id] = job

    def progress(done: int, total: int):
        job["done"], job["total"] = done, total

    def work():
        try:
            job["result"] = _run_review(req, progress, job["cancel"])
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


@app.post("/api/fs/scan")
def fs_scan(req: PathRequest):
    try:
        r = folder.scan(req.path, _exclude(req))
        last = folder_ai.last_review(req.path)
        r["last_review"] = ({"time": last["time"], "issues": len(last["issues"]),
                             "pending": len(last.get("pending", []))} if last else None)
        return r
    except Exception as e:
        return {"error": str(e)}


def _port_free(port: int) -> bool:
    import socket
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
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.loads(r.read()).get("app") == "codecheck"
    except Exception:
        return False


def main():
    import threading
    import webbrowser

    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
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
        for candidate in range(port + 1, port + 50):
            if _port_free(candidate):
                print(f"端口 {port} 被其他程序占用，改用 {candidate}。")
                port = candidate
                break
    url = f"http://127.0.0.1:{port}"
    print(f"代码检查助手已启动：{url}")
    print("用完后关闭这个窗口即可停止。")
    if not os.environ.get("CODECHECK_NO_BROWSER"):  # 更新后重启时不再开新标签页
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()  # 自动打开浏览器
    # 只监听本机：这个服务会运行代码，不要暴露到公网
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
