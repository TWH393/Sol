"""Own one bundled Ollama process tree through a Windows job and stdin lease.

Standard library only: launched directly with -I -S, never imports the engine.
The supervisor holds the sole job handle. Its exit (including forced exit)
therefore releases Ollama and every runner, without process-name/PID searches.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading


def protect_process_tree():
    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel.SetHandleInformation.restype = wintypes.BOOL
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
    for operation, args in (
        (kernel.SetHandleInformation, (job, 1, 0)),  # never inherit the job handle
        (kernel.SetInformationJobObject, (job, 9, ctypes.byref(limits), ctypes.sizeof(limits))),
        # Join BEFORE creating Ollama: no unprotected spawn/assignment race.
        (kernel.AssignProcessToJobObject, (job, kernel.GetCurrentProcess())),
    ):
        if not operation(*args):
            raise ctypes.WinError(ctypes.get_last_error())
    return job


def status(path, nonce, **data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"nonce": nonce, "pid": os.getpid(), **data}), "utf-8")
    temporary.replace(path)


def watch_owner():
    try:
        sys.stdin.buffer.read(1)
    finally:
        # Closes our sole job handle even if the owner crashed or was killed.
        os._exit(0)


def main():
    marker, nonce = Path(sys.argv[1]), sys.argv[2]
    try:
        if os.name != "nt":
            raise RuntimeError("本地模型进程组保护只支持Windows")
        runtime = Path(__file__).resolve().parents[2] / "runtime/ollama/ollama.exe"
        if not runtime.is_file():
            raise FileNotFoundError(f"缺少Sol私有本地模型运行时：{runtime}")
        protect_process_tree()  # OS retains the non-inherited handle until os._exit.
        process = subprocess.Popen(
            [str(runtime), "serve"], stdin=subprocess.DEVNULL,
            stdout=sys.stdout, stderr=sys.stderr, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        status(marker, nonce, state="started", child_pid=process.pid, protected=True)
        threading.Thread(target=watch_owner, daemon=True).start()
        code = process.wait()
        status(marker, nonce, state="exited", child_pid=process.pid, exit_code=code)
    except BaseException as exc:
        code = 1
        print(f"Sol本地模型服务启动失败：{exc}", file=sys.stderr, flush=True)
        try:
            status(marker, nonce, state="error", error=str(exc))
        except OSError:
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code if 0 <= code <= 255 else 1)


if __name__ == "__main__":
    main()
