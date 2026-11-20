"""Run a module as ``__main__`` in this process, then record its wall time and peak memory.

    python -m bench.measure result.json -- archive_mirror mirror http://127.0.0.1:8080/

The time starts before the module is imported, so it includes the module's own imports
but not the interpreter's start. Peak memory is the process's peak resident set (the
peak working set from ``GetProcessMemoryInfo`` on Windows, ``ru_maxrss`` elsewhere), read
after the run: no sampling, nothing missed between samples. Child processes are not
included.
"""

import json
import runpy
import sys
import time
from pathlib import Path


def peak_rss_bytes() -> int:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS: order and types matter
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("peak_working_set", ctypes.c_size_t),
                ("working_set", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.peak_working_set)
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # Linux reports KiB


def main() -> None:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        sys.exit("usage: python -m bench.measure RESULT.json -- MODULE [ARGS...]")
    result_path, module = Path(sys.argv[1]), sys.argv[3]
    sys.argv = [module, *sys.argv[4:]]
    started = time.perf_counter()
    code = 0
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    seconds = time.perf_counter() - started
    result = {"exit": code, "seconds": round(seconds, 3), "peak_rss": peak_rss_bytes()}
    result_path.write_text(json.dumps(result), encoding="utf-8")
    sys.exit(code)


if __name__ == "__main__":
    main()
