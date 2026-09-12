"""真实 EXE 空闲内存验证（独立、ctypes 实现，无需 psutil）：

1. 清理可能残留的 App / LHM 进程（干净起点）。
2. 启动桌面上的 PC用电电费计算器_v18.48.exe（onefile 冷启动）。
3. 等 ~15s 暖机（不打开任何面板，验证「空闲不拉起 LHM」）。
4. 用 ctypes 快照：App 进程 RSS 是否 ≤ 100MB，且「不存在 LibreHardwareMonitor 进程」。
5. 关闭 App 与 LHM，清理。
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

EXE = r"C:\Users\Administrator\Desktop\PC用电电费计算器_v18.49.exe"
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wt.ULONG)),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", wt.CHAR * MAX_PATH)]


class PMEM(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t), ("PrivateUsage", ctypes.c_size_t)]


def snap():
    s = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32(); pe.dwSize = ctypes.sizeof(pe)
    out = []
    if kernel32.Process32First(s, ctypes.byref(pe)):
        while True:
            out.append((pe.th32ProcessID, pe.szExeFile.decode("mbcs", "ignore")))
            if not kernel32.Process32Next(s, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(s)
    return out


def mem(pid):
    h = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        return None, None
    cnt = PMEM(); cnt.cb = ctypes.sizeof(cnt)
    rss = priv = None
    if psapi.GetProcessMemoryInfo(h, ctypes.byref(cnt), cnt.cb):
        rss = cnt.WorkingSetSize / 1048576.0
        priv = cnt.PrivateUsage / 1048576.0
    kernel32.CloseHandle(h)
    return rss, priv


def kill(name):
    subprocess.run(["taskkill", "/F", "/IM", name], stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, creationflags=0x08000000, timeout=8)


def main():
    assert os.path.exists(EXE), "未找到 EXE: " + EXE
    print("[clean] 清理残留...")
    kill("LibreHardwareMonitor.exe")
    kill(os.path.basename(EXE))
    time.sleep(2)

    print("[launch] 启动 EXE（冷启动，不打开面板）...")
    subprocess.Popen([EXE], creationflags=0x08000000)  # CREATE_NO_WINDOW：不弹控制台
    time.sleep(15)

    procs = snap()
    app_rss = None; app_priv = None; app_pid = None
    lhm = []
    for pid, name in procs:
        if "PC用电电费计算器" in name and "LibreHardwareMonitor" not in name:
            rss, priv = mem(pid)
            if rss and (app_rss is None or rss > app_rss):
                app_rss, app_priv, app_pid = rss, priv, pid
        elif "LibreHardwareMonitor" in name:
            lhm.append((pid, name))
    print("  App 进程 rss=%.1fMB private=%.1fMB (pid=%s)" % (app_rss or 0, app_priv or 0, app_pid))
    print("  LHM 进程数 = %d" % len(lhm))
    for pid, name in lhm:
        rss, _ = mem(pid)
        print("    LHM pid=%s rss=%.1fMB" % (pid, rss or 0))

    print("[check] 空闲约束：App rss ≤ 100MB 且 无 LHM 进程")
    ok = (app_rss is not None and app_rss <= 100.0 and len(lhm) == 0)
    print("  =>", "PASS" if ok else "FAIL")
    if app_rss and app_rss > 100.0:
        print("  !! App rss 超 100MB")
    if lhm:
        print("  !! 空闲仍有 LHM 进程（应仅按需运行时才出现）")

    print("[cleanup] 关闭 App + LHM")
    kill(os.path.basename(EXE))
    kill("LibreHardwareMonitor.exe")
    print("EXE_IDLE_VERIFY_DONE" if ok else "EXE_IDLE_VERIFY_FAIL")


if __name__ == "__main__":
    main()
