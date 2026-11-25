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


MAX_FILE_BYTES = 10 * 1024 * 1024  # 被运行的代码最多写 10MB 的文件


def _posix_limits(lang_key: str, timeout: int, memory_mb: int):
    """没有 prlimit 命令时的备用方案（主要是 macOS）。fork 之后只做 setrlimit，尽量少。"""
    import resource

    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 1, timeout + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
        if lang_key != "javascript":  # V8 会预留大量虚拟内存，node 用 --max-old-space-size 控制
            resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024,) * 2)
    return apply


def _prlimit_prefix(lang_key: str, timeout: int, memory_mb: int) -> list[str]:
    """Linux：用 prlimit 命令设置资源限制，避免在多线程程序里使用 preexec_fn。"""
    exe = shutil.which("prlimit")
    if not exe:
        return []
    args = [exe, f"--cpu={timeout + 1}", f"--fsize={MAX_FILE_BYTES}"]
    if lang_key != "javascript":
        args.append(f"--as={memory_mb * 1024 * 1024}")
    return args + ["--"]


class _WinJob:
    """Windows 作业对象：限制内存和进程数；超时时连同子进程一起结束。任何一步失败都只是不启用。"""

    def __init__(self, memory_mb: int):
        self.handle = None
        try:
            import ctypes
            from ctypes import wintypes

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

            u32 = ctypes.c_uint32  # Windows 的 DWORD 是 4 字节

            class Basic(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION（64 位下 64 字节）
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", u32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", u32),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", u32), ("SchedulingClass", u32)]

            class Extended(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION（64 位下 144 字节）
                _fields_ = [("Basic", Basic), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.k32 = k32
            h = k32.CreateJobObjectW(None, None)
            if not h:
                return
            info = Extended()
            # KILL_ON_JOB_CLOSE | JOB_MEMORY | ACTIVE_PROCESS | DIE_ON_UNHANDLED_EXCEPTION
            info.Basic.LimitFlags = 0x2000 | 0x200 | 0x8 | 0x400
            info.Basic.ActiveProcessLimit = 32
            info.JobMemoryLimit = max(memory_mb * 2, 512) * 1024 * 1024
            if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(h)
                return
            self.handle = h
        except Exception:
            self.handle = None

    def assign(self, proc) -> bool:
        if not self.handle:
            return False
        try:
            return bool(self.k32.AssignProcessToJobObject(self.handle, int(proc._handle)))
        except Exception:
            return False

    def kill(self) -> bool:
        if not self.handle:
            return False
        try:
            return bool(self.k32.TerminateJobObject(self.handle, 1))
        except Exception:
            return False

    def close(self):
        if self.handle:
            try:
                self.k32.CloseHandle(self.handle)  # KILL_ON_JOB_CLOSE：还活着的子进程也会被结束
            except Exception:
                pass
            self.handle = None


def _kill_tree(proc, job: "_WinJob | None"):
    if sys.platform == "win32":
        if job and job.kill():
            return
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=NO_WINDOW)
        return
    try:
        os.killpg(proc.pid, 9)  # start_new_session=True，进程组号就是 pid
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


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
              extra_args: list | None = None, workdir: str | None = None) -> RunResult:
    cmd = [part.replace("{file}", path) for part in lang.run_cmd] + list(extra_args or [])
    if cmd[0] == "python3":
        cmd[0] = console_python()
    if not (os.path.isabs(cmd[0]) and os.path.isfile(cmd[0])) and not shutil.which(cmd[0]):
        need = "Node.js（https://nodejs.org 下载 LTS 版本）" if lang.key == "javascript" else lang.name
        raise RuntimeError(f"找不到 {cmd[0]}，没法运行 {lang.name} 代码。请先安装 {need}")
    cwd = workdir if workdir and os.path.isdir(workdir) else os.path.dirname(path)
    env = _clean_env(cwd)
    prefix, preexec, job = [], None, None
    if sys.platform == "win32":
        job = _WinJob(memory_mb)
    else:
        prefix = _prlimit_prefix(lang.key, timeout, memory_mb)
        if not prefix:
            preexec = _posix_limits(lang.key, timeout, memory_mb)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(prefix + cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=NO_WINDOW, start_new_session=sys.platform != "win32",
                                cwd=cwd, env=env, text=True, encoding="utf-8",
                                errors="replace", preexec_fn=preexec)
    except FileNotFoundError:
        if job:
            job.close()
        need = "Node.js（https://nodejs.org 下载 LTS 版本）" if lang.key == "javascript" else lang.name
        raise RuntimeError(f"找不到 {os.path.basename(cmd[0])}，没法运行 {lang.name} 代码。请先安装 {need}") from None
    if job:
        job.assign(proc)
    timed_out = False
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc, job)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # 极少数情况：输出管道被孙进程占着
            proc.kill()
            out, err = "", ""
    finally:
        if job:
            job.close()
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
    import uuid
    name = f"codecheck-{uuid.uuid4().hex[:12]}"
    # 容器里也套一层 timeout，到时间就在容器内部结束（退出码 137），容器随之停止并被 --rm 删除
    cmd = ["docker", "run", "--rm", "-i", "--name", name, "--network", "none", f"--memory={memory_mb}m",
           "--cpus=1", "--pids-limit=64", "--read-only", "--tmpfs", "/tmp:size=16m",
           "-v", f"{folder}:/code:ro", "-w", "/tmp", lang.docker_image,
           "timeout", "-s", "KILL", str(timeout), *inner]
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout + 20,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        dur = int((time.monotonic() - start) * 1000)
        if p.returncode == 125:
            raise RuntimeError("Docker 启动失败：" + p.stderr.strip())
        if p.returncode == 137:
            return RunResult(None, _clip(p.stdout), _clip(p.stderr), dur, True, "docker",
                             f"超过 {timeout} 秒被强制停止（可能是死循环、在等待输入，或内存超限）")
        return RunResult(p.returncode, _clip(p.stdout), _clip(p.stderr), dur, False, "docker")
    except subprocess.TimeoutExpired as e:
        # 本机的 docker 命令超时只会结束客户端，容器本身要单独停掉
        subprocess.run(["docker", "kill", name], capture_output=True, creationflags=NO_WINDOW, timeout=30)
        dur = int((time.monotonic() - start) * 1000)
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return RunResult(None, _clip(out), _clip(err), dur, True, "docker", "运行超时，容器已被强制停止")


SANDBOXES = ("local", "docker")


def run(lang: Language, path: str, stdin: str = "", timeout: int = 5, memory_mb: int = 256,
        mode: str = "local", sandbox: str | None = None, extra_args: list | None = None,
        workdir: str | None = None) -> RunResult:
    mode = sandbox or mode
    if mode not in SANDBOXES:  # 写错了不能悄悄改成本机运行
        raise ValueError(f"未知的运行方式：{mode}（只能是 local 或 docker）")
    if mode == "docker":
        return run_docker(lang, path, stdin, timeout, memory_mb, extra_args)
    return run_local(lang, path, stdin, timeout, memory_mb, extra_args, workdir)
