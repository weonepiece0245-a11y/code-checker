"""在受限环境里运行代码，捕获输出、报错、耗时。

两种模式：
- local：本机子进程 + 超时 + CPU/内存/文件大小限制。适合运行自己写的代码。
- docker：容器里运行，断网、只读、限内存。适合运行来路不明的代码（需要安装并启动 Docker）。
"""
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict

from .languages import Language
from .proc import NO_WINDOW, console_python


@dataclass
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    mode: str
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self):
        d = asdict(self)
        d["ok"] = self.ok
        return d


MAX_OUTPUT = 20_000  # 输出最多保留多少字符


def _clip(s: str) -> str:
    return s if len(s) <= MAX_OUTPUT else s[:MAX_OUTPUT] + f"\n...（输出过长，已截断，共 {len(s)} 字符）"


def _limits(lang_key: str, timeout: int, memory_mb: int):
    if sys.platform == "win32":
        return None

    def apply():
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 1, timeout + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024,) * 2)  # 最多写 10MB 文件
        if lang_key != "javascript":  # V8 会预留大量虚拟内存，node 用 --max-old-space-size 控制
            resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
        os.setsid()  # 独立进程组，超时时可以整组杀掉
    return apply


# Windows 上缺少这些变量时 Python/Node 无法启动
_WIN_KEEP = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC", "USERPROFILE",
             "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS")


def _clean_env(workdir: str) -> dict:
    """只给被运行的代码最少的环境变量，不把 API key 等敏感信息传进去。"""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
           "LANG": "C.UTF-8", "HOME": workdir}
    if sys.platform == "win32":
        for k in _WIN_KEEP:
            if k in os.environ:
                env[k] = os.environ[k]
    return env


def run_local(lang: Language, path: str, stdin: str, timeout: int, memory_mb: int,
              extra_args: list | None = None) -> RunResult:
    cmd = [part.replace("{file}", path) for part in lang.run_cmd] + list(extra_args or [])
    if cmd[0] == "python3":
        cmd[0] = console_python()
    env = _clean_env(os.path.dirname(path))
    start = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW,
                            cwd=os.path.dirname(path), env=env, text=True, encoding="utf-8",
                            errors="replace", preexec_fn=_limits(lang.key, timeout, memory_mb))
    timed_out = False
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, AttributeError, PermissionError, OSError):
            proc.kill()
        out, err = proc.communicate()
    dur = int((time.monotonic() - start) * 1000)
    note = f"超过 {timeout} 秒被强制停止（可能是死循环或在等待输入）" if timed_out else ""
    code = proc.returncode
    if code is not None and code < 0 and not timed_out:
        note = {-9: "进程被杀死（可能是内存或 CPU 超限）", -24: "CPU 时间超限",
                -25: "写入文件过大"}.get(code, f"进程被信号 {-code} 终止")
    return RunResult(None if timed_out else code, _clip(out), _clip(err), dur, timed_out, "local", note)


def run_docker(lang: Language, path: str, stdin: str, timeout: int, memory_mb: int,
               extra_args: list | None = None) -> RunResult:
    if not shutil.which("docker"):
        raise RuntimeError("没有安装 Docker")
    folder, fname = os.path.split(path)
    inner = [part.replace("{file}", f"/code/{fname}") for part in lang.run_cmd]
    inner += [f"/code/{a}" for a in (extra_args or [])]
    cmd = ["docker", "run", "--rm", "-i", "--network", "none", f"--memory={memory_mb}m",
           "--cpus=1", "--pids-limit=64", "--read-only", "--tmpfs", "/tmp:size=16m",
           "-v", f"{folder}:/code:ro", "-w", "/tmp", lang.docker_image, *inner]
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout + 15,
                           creationflags=NO_WINDOW,
                           encoding="utf-8", errors="replace")
        dur = int((time.monotonic() - start) * 1000)
        if p.returncode == 125:
            raise RuntimeError("Docker 启动失败：" + p.stderr.strip())
        return RunResult(p.returncode, _clip(p.stdout), _clip(p.stderr), dur, False, "docker")
    except subprocess.TimeoutExpired as e:
        dur = int((time.monotonic() - start) * 1000)
        return RunResult(None, _clip(e.stdout or ""), _clip(e.stderr or ""), dur, True, "docker",
                         "运行超时，被强制停止")


def run(lang: Language, path: str, stdin: str = "", timeout: int = 5, memory_mb: int = 256,
        mode: str = "local", sandbox: str | None = None, extra_args: list | None = None) -> RunResult:
    mode = sandbox or mode
    if mode == "docker":
        return run_docker(lang, path, stdin, timeout, memory_mb, extra_args)
    return run_local(lang, path, stdin, timeout, memory_mb, extra_args)
