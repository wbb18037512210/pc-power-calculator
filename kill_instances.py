# -*- coding: utf-8 -*-
"""打包前关闭所有正在运行的实例。

为什么要这一步：
  桌面上的 exe 被正在运行的进程占用着文件映射，直接覆盖会失败
  （表现为「文件在使用中」，而脚本不报错、最后启动的还是旧版）。
  所以每次打包前必须先把所有实例关掉。

用法：python kill_instances.py
"""
from __future__ import annotations

import sys

TARGETS = ("PC用电电费计算器", "PC电费_debug", "LibreHardwareMonitor")   # 前缀匹配：产物带版本号也能杀到；含 LHM 守护进程


def _running_names() -> list[str]:
    """tasklist 列出进程名，返回命中前缀的完整镜像名（如 PC用电电费计算器_v18.17.exe）。"""
    import subprocess
    try:
        r = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=20,
            encoding="gbk", errors="replace",
            creationflags=0x08000000,
        )
    except Exception:
        return []
    names = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        try:
            name = line.split('","')[0].strip('"')
        except Exception:
            continue
        if any(name.startswith(p) for p in TARGETS) and name not in names:
            names.append(name)
    return names


def kill() -> int:
    import subprocess
    killed = 0
    # 先按镜像名精确杀（覆盖无版本号的旧命名），再按前缀枚举杀（覆盖带版本号的新命名）
    names = list(TARGETS) + [n for n in _running_names() if n not in TARGETS]
    for name in names:
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/IM", name],
                capture_output=True, timeout=20,
                encoding="gbk", errors="replace",   # cmd 输出是 GBK，按 utf-8 解会崩
                creationflags=0x08000000,   # CREATE_NO_WINDOW：不弹黑框
            )
            out = (r.stdout or "") + (r.stderr or "")
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "PID" in line or "成功" in line or "SUCCESS" in line.upper():
                    killed += 1
                    print("  %s" % line)
        except subprocess.TimeoutExpired:
            print("  [超时] %s" % name)
        except FileNotFoundError:
            print("  [跳过] 未找到 taskkill")
            break
        except Exception as e:
            print("  [跳过] %s: %r" % (name, e))
    return killed


if __name__ == "__main__":
    n = kill()
    print("已结束 %d 个进程" % n)
    sys.exit(0)
