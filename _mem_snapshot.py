"""Safe, read-only memory snapshot of currently running app + LHM processes.

Pure stdlib (ctypes + ToolHelp API) — no psutil needed.
No killing, no relaunch, no app.lock changes.
Reports both WorkingSet (RSS, what Task Manager shows) and Private bytes.
"""
import ctypes
from ctypes import wintypes as wt

kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wt.ULONG)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.CHAR * MAX_PATH),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def enum_processes():
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value:
        return []
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(pe)
    out = []
    if kernel32.Process32First(snap, ctypes.byref(pe)):
        while True:
            out.append((pe.th32ProcessID, pe.szExeFile.decode("mbcs", "ignore")))
            if not kernel32.Process32Next(snap, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(snap)
    return out


def mem_info(pid):
    h = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not h:
        return None, None
    cnt = PROCESS_MEMORY_COUNTERS_EX()
    cnt.cb = ctypes.sizeof(cnt)
    rss = None
    priv = None
    if psapi.GetProcessMemoryInfo(h, ctypes.byref(cnt), cnt.cb):
        rss = cnt.WorkingSetSize / (1024 * 1024)
        priv = cnt.PrivateUsage / (1024 * 1024)
    kernel32.CloseHandle(h)
    return rss, priv


targets = ("PC用电电费计算器", "LibreHardwareMonitor", "LibreHardwareMonitorWeb")
rows = []
for pid, name in enum_processes():
    if any(t in name for t in targets):
        rss, priv = mem_info(pid)
        if rss is not None:
            rows.append((pid, name, rss, priv))

if not rows:
    print("NO_TARGET_PROCESSES_RUNNING")
else:
    total_rss = total_priv = 0.0
    for pid, name, rss, priv in sorted(rows, key=lambda r: r[2], reverse=True):
        priv_s = f"{priv:6.1f}MB" if priv is not None else "   n/a"
        print(f"pid={pid:<6} {name:<32} rss={rss:6.1f}MB  private={priv_s}")
        total_rss += rss
        if priv is not None:
            total_priv += priv
    print(f"--- TOTAL rss={total_rss:.1f}MB  private={total_priv:.1f}MB ---")
