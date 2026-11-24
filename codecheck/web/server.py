"""网页版：python -m codecheck.web.server  然后浏览器打开 http://127.0.0.1:8765"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..ai import pick_backend
from ..core import check_code

app = FastAPI(title="代码检查助手")
PAGE = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


class CheckRequest(BaseModel):
    code: str = Field(max_length=200_000)
    language: str | None = None
    stdin: str = ""
    run: bool = True
    timeout: int = Field(5, ge=1, le=60)
    sandbox: str = "local"
    ai: str = "auto"
    explain_lang: str = "简体中文"


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/api/info")
def info():
    return {"ai_auto": pick_backend("auto"), "version": __version__}


@app.post("/api/check")
def check(req: CheckRequest):
    return check_code(req.code, None, req.language or None, req.stdin, req.run, req.timeout,
                      sandbox=req.sandbox, ai_backend=req.ai, explain_lang=req.explain_lang)


def _port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
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
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()  # 自动打开浏览器
    # 只监听本机：这个服务会运行代码，不要暴露到公网
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
