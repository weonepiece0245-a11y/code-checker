"""网页版：python -m codecheck.web.server  然后浏览器打开 http://127.0.0.1:8765"""
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, files, folder, testgen, updater
from ..ai import pick_backend
from ..core import check_code

app = FastAPI(title="代码检查助手")
PAGE = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
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
    return await call_next(request)


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class CheckRequest(BaseModel):
    code: str = Field(max_length=200_000)
    language: str | None = None
    stdin: str = ""
    run: bool = True
    timeout: int = Field(5, ge=1, le=60)
    sandbox: str = "local"
    ai: str = "auto"
    explain_lang: str = "简体中文"
    workdir: str | None = None


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/api/info")
def info():
    return {"ai_auto": pick_backend("auto"), "version": __version__}


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
    language: str | None = None
    ai: str = "auto"
    explain_lang: str = "简体中文"


@app.post("/api/tests/generate")
def tests_generate(req: GenTestsRequest):
    try:
        return testgen.generate(req.code, req.language or None, req.explain_lang, req.ai)
    except Exception as e:
        return {"error": str(e)}


class RunTestsRequest(BaseModel):
    code: str = Field(max_length=200_000)
    language: str | None = None
    tests: list[dict]
    timeout: int = Field(5, ge=1, le=60)
    sandbox: str = "local"


@app.post("/api/tests/run")
def tests_run(req: RunTestsRequest):
    try:
        return testgen.run(req.code, req.tests, req.language or None, req.timeout, req.sandbox)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/check")
def check(req: CheckRequest):
    return check_code(req.code, None, req.language or None, req.stdin, req.run, req.timeout,
                      sandbox=req.sandbox, ai_backend=req.ai, explain_lang=req.explain_lang,
                      workdir=req.workdir)


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


@app.post("/api/fs/scan")
def fs_scan(req: PathRequest):
    try:
        return folder.scan(req.path)
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
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=2) as r:
            return "ai_auto" in json.loads(r.read())
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
