"""在受限环境里运行代码，捕获输出、报错、耗时。

两种模式：
- local：本机子进程 + 超时 + CPU/内存/文件大小限制。适合运行自己写的代码。
- docker：容器里运行，断网、只读、限内存。适合运行来路不明的代码（需要安装并启动 Docker）。
"""
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict

from .languages import Language
from .proc import NO_WINDOW, console_python, kill_tree


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

# “在原位置运行”的引导程序：代码内容来自临时文件（编辑器里的最新内容，可能还没保存），
# 但让它以为自己就是原来那个文件——能 import 同目录的模块，__file__、报错里的路径和行号都对得上。
_PY_BOOT = (
    "import os,sys,types\n"
    "_t,_s=sys.argv[1],sys.argv[2]\n"
    "_c=open(_t,encoding='utf-8').read()\n"
    # sys.path[0] 原本是引导文件所在的临时文件夹，换成原文件所在的文件夹
    "sys.path[0]=os.path.dirname(os.path.abspath(_s))\n"
    "sys.argv=[_s]\n"
    # 建一个真正的 __main__ 模块并放进 sys.modules：pickle、multiprocessing、dataclasses、
    # typing.get_type_hints 等按 __main__ 找名字的功能才能正常工作，和直接运行一样
    "_m=types.ModuleType('__main__')\n"
    "_m.__file__=_s\n"
    "_m.__builtins__=__builtins__\n"
    "sys.modules['__main__']=_m\n"
    "del os,types,_t\n"
    "exec(compile(_c,_s,'exec'),_m.__dict__)\n"
)
_JS_BOOT = (
    "const Module=require('module'),fs=require('fs'),path=require('path');"
    "const [t,s]=process.argv.slice(1);const c=fs.readFileSync(t,'utf8');"
    "const m=new Module(s,null);m.filename=s;m.id='.';m.paths=Module._nodeModulePaths(path.dirname(s));"
    "process.argv=[process.argv[0],s];process.mainModule=m;m._compile(c,s);"
)
BOOT_NAME = "_codecheck_run_.py"
_ESM = re.compile(r"^\s*(import\s.+\sfrom\s|import\s+['\"]|export\s)", re.M)


def _in_place_cmd(lang: Language, tmp_path: str, source_path: str, code: str) -> list[str] | None:
    """返回“在原位置运行”的命令；做不到（例如 ES 模块）时返回 None，退回普通方式。"""
    if lang.key == "python":
        # 引导程序写成单独的文件（不用 -c）：否则它和用户代码里 exec() 的字符串都叫 "<string>"，
        # 报错时 Python 会把引导程序的代码行当成用户的显示出来
        boot = os.path.join(os.path.dirname(tmp_path), BOOT_NAME)
        with open(boot, "w", encoding="utf-8") as f:
            f.write(_PY_BOOT)
        return [console_python(), boot, tmp_path, source_path]
    if lang.key == "javascript" and not _ESM.search(code) and not source_path.lower().endswith(".mjs"):
        return ["node", "--max-old-space-size=256", "-e", _JS_BOOT, tmp_path, source_path]
    return None


def _clean_boot_frames(text: str) -> str:
    """去掉报错里属于引导程序的那一层，只留用户代码的。

    引导程序那一层一定是紧跟在 “Traceback (most recent call last):” 后面的第一层（File ".../_codecheck_run_.py"）。
    只删这一层；用户代码自己用 exec()/eval() 产生的 "<string>" 层要保留。
    """
    lines = text.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        if line.rstrip().endswith("Traceback (most recent call last):") and i < len(lines) \
                and re.match(r'\s*File ".*' + re.escape(BOOT_NAME) + r'", line \d+, in <module>', lines[i]):
            i += 1  # 跳过这一层的标题，以及它下面缩进更深的代码行和 ~~~^^^ 标记
            while i < len(lines) and lines[i].startswith("    ") and not lines[i].lstrip().startswith("File "):
                i += 1
    return "".join(out)


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


CREATE_SUSPENDED = 0x00000004


def _resume(proc) -> bool:
    """让以“暂停”状态启动的 Windows 进程开始运行（NtResumeProcess 恢复进程里的所有线程）。"""
    try:
        import ctypes
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        return ntdll.NtResumeProcess(ctypes.c_void_p(int(proc._handle))) == 0  # 0 = STATUS_SUCCESS
    except Exception:
        return False


def _kill_tree(proc, job: "_WinJob | None"):
    if job and job.kill():  # Windows：作业对象一次结束整组
        return
    kill_tree(proc)


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
              extra_args: list | None = None, workdir: str | None = None,
              source_path: str | None = None) -> RunResult:
    cmd = [part.replace("{file}", path) for part in lang.run_cmd] + list(extra_args or [])
    if cmd[0] == "python3":
        cmd[0] = console_python()
    in_place = False
    if source_path and not extra_args and os.path.isdir(os.path.dirname(source_path)):
        with open(path, encoding="utf-8") as f:
            boot = _in_place_cmd(lang, path, source_path, f.read())
        if boot:
            cmd, in_place = boot, True
            workdir = os.path.dirname(source_path)
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
    def launch(suspended: bool):
        flags = NO_WINDOW | (CREATE_SUSPENDED if suspended else 0)
        return subprocess.Popen(prefix + cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=flags, start_new_session=sys.platform != "win32",
                                cwd=cwd, env=env, text=True, encoding="utf-8",
                                errors="replace", preexec_fn=preexec)

    start = time.monotonic()
    limit_note = ""
    try:
        # Windows：先以“暂停”状态启动，加进作业对象后再让它开始运行，
        # 这样它来不及在资源限制生效前启动子进程
        suspended = bool(job and job.handle)
        proc = launch(suspended)
        if job:
            assigned = job.assign(proc)
            if suspended and not _resume(proc):
                # 恢复运行失败（极少见）：结束它，改用普通方式启动，再尽量加进作业对象
                _kill_tree(proc, job)
                proc.wait()
                proc = launch(False)
                assigned = job.assign(proc)
            if not assigned:
                limit_note = "（注意：这次没能启用 Windows 的内存和进程数限制，只有时间限制）"
    except FileNotFoundError:
        if job:
            job.close()
        need = "Node.js（https://nodejs.org 下载 LTS 版本）" if lang.key == "javascript" else lang.name
        raise RuntimeError(f"找不到 {os.path.basename(cmd[0])}，没法运行 {lang.name} 代码。请先安装 {need}") from None
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
    if in_place:
        err = _clean_boot_frames(err)
    if limit_note:
        note = (note + " " + limit_note).strip()
    return RunResult(None if timed_out else code, _clip(out), _clip(err), dur, timed_out, "local", note)


_PULLED: set[str] = set()


def _ensure_image(image: str) -> str:
    """确认 Docker 运行环境已经下载好；第一次用时先下载（可能要几分钟），不算进代码的运行时间。"""
    if image in _PULLED:
        return ""
    r = subprocess.run(["docker", "image", "inspect", image], capture_output=True, creationflags=NO_WINDOW, timeout=30)
    if r.returncode == 0:
        _PULLED.add(image)
        return ""
    try:
        p = subprocess.run(["docker", "pull", image], capture_output=True, text=True, creationflags=NO_WINDOW,
                           timeout=900, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"下载 Docker 运行环境 {image} 超时，请检查网络后再试") from None
    if p.returncode != 0:
        raise RuntimeError(f"下载 Docker 运行环境 {image} 失败：" + (p.stderr or p.stdout).strip()[-300:])
    _PULLED.add(image)
    return f"（第一次使用，已下载 Docker 运行环境 {image}）"


def run_docker(lang: Language, path: str, stdin: str, timeout: int, memory_mb: int,
               extra_args: list | None = None) -> RunResult:
    if not shutil.which("docker"):
        raise RuntimeError("没有安装 Docker")
    pulled_note = _ensure_image(lang.docker_image)
    folder, fname = os.path.split(path)
    inner = [part.replace("{file}", f"/code/{fname}") for part in lang.run_cmd]
    inner += [f"/code/{a}" for a in (extra_args or [])]
    import uuid
    name = f"codecheck-{uuid.uuid4().hex[:12]}"
    # 不用 --rm：结束后先查容器的真实状态（有没有真的启动、是不是内存超限），再删掉容器。
    # 容器里也套一层 timeout：到时间先请程序结束（退出码 124），2 秒后还不结束就强制结束（137）
    cmd = ["docker", "run", "-i", "--name", name, "--network", "none", f"--memory={memory_mb}m",
           "--cpus=1", "--pids-limit=64", "--read-only", "--tmpfs", "/tmp:size=16m",
           "-v", f"{folder}:/code:ro", "-w", "/tmp", lang.docker_image,
           "timeout", "-s", "TERM", "-k", "2", str(timeout), *inner]
    start = time.monotonic()
    killed_by_us = False
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout + 30,
                           creationflags=NO_WINDOW, encoding="utf-8", errors="replace")
        out, err, code = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        # 本机的 docker 命令超时只会结束客户端，容器本身要单独停掉
        subprocess.run(["docker", "kill", name], capture_output=True, creationflags=NO_WINDOW, timeout=30)
        killed_by_us = True
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        code = None
    dur = int((time.monotonic() - start) * 1000)
    state = _container_state(name)  # 删掉容器前先看它的状态
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, creationflags=NO_WINDOW, timeout=30)
    if state is None:  # 容器根本没建起来：是 Docker 自己出了问题，不是用户程序的退出码
        raise RuntimeError("Docker 启动失败：" + (err.strip() or "未知原因")[-300:])
    if killed_by_us:
        return RunResult(None, _clip(out), _clip(err), dur, True, "docker", "运行超时，容器已被强制停止")
    if state["oom"]:
        return RunResult(code, _clip(out), _clip(err), dur, False, "docker",
                         (f"内存超过 {memory_mb}MB，被系统强制结束" + pulled_note).strip())
    if code in (124, 137) and dur >= timeout * 1000:
        return RunResult(None, _clip(out), _clip(err), dur, True, "docker",
                         (f"超过 {timeout} 秒被强制停止（可能是死循环或在等待输入）" + pulled_note).strip())
    return RunResult(code, _clip(out), _clip(err), dur, False, "docker", pulled_note)


def _container_state(name: str) -> dict | None:
    """容器结束后的状态：{"oom": 是否因内存超限被结束}；容器不存在（没建起来）返回 None。"""
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.OOMKilled}}", name], capture_output=True,
                           text=True, creationflags=NO_WINDOW, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {"oom": False}
    if r.returncode != 0:
        return None
    return {"oom": r.stdout.strip().lower() == "true"}


SANDBOXES = ("local", "docker")


def run(lang: Language, path: str, stdin: str = "", timeout: int = 5, memory_mb: int = 256,
        mode: str = "local", sandbox: str | None = None, extra_args: list | None = None,
        workdir: str | None = None, source_path: str | None = None) -> RunResult:
    mode = sandbox or mode
    if mode not in SANDBOXES:  # 写错了不能悄悄改成本机运行
        raise ValueError(f"未知的运行方式：{mode}（只能是 local 或 docker）")
    if mode == "docker":
        return run_docker(lang, path, stdin, timeout, memory_mb, extra_args)
    return run_local(lang, path, stdin, timeout, memory_mb, extra_args, workdir, source_path)
