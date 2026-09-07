# -*- coding: utf-8 -*-
"""本机硬件配置探测 + 实时负载采样（Windows / PowerShell / WMI）。

探测逻辑在真实 Windows PC 上可用；在沙箱/虚拟机中返回对应虚拟硬件信息，
不影响代码逻辑本身。GPU 实时功耗优先用 nvidia-smi 直读（N 卡真实值），
其余部件按功耗模型估算。
"""
from __future__ import annotations

import json
import os
import subprocess
import re
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

try:
    import psutil
except Exception:
    psutil = None


# ---------------------------------------------------------------------------
# v18.31 硬件识别增强（移植自 TubaTools HardwareInfoService）
# ---------------------------------------------------------------------------
# 虚拟/伪显卡黑名单。原来只有 Virtual/Basic/Microsoft 三个词，实测会漏掉
# 「GameViewer Display Adapter」（远控/串流虚拟显卡，国产远控软件常见）和
# 「Idd Desk Adapter」（间接显示驱动）—— 一旦被当成主显卡，GTX 1080 的 180W
# 会被算成 75W（-58%）。关键词全部大写比对，调用方负责 casefold。
VIRTUAL_GPU_KEYWORDS = (
    "VIRTUAL",          # Virtual Display / Virtual Adapter / Virtual GPU
    "BASIC",            # Microsoft Basic Render Driver
    "MICROSOFT",        # 微软系软渲染 / 远程桌面适配器
    "DDA WRAPPER",      # Discrete Device Assignment（显卡直通）
    "IDD DESK",         # Indirect Display Driver
    "GAMEVIEWER",       # 远控串流虚拟显卡（实测本机存在）
    "HONOR VIRTUAL",
    "REMOTE DISPLAY",   # RDP / 远程桌面
    "虚拟",
)

# Win32_SystemEnclosure.ChassisTypes 中表示便携机的取值
# 8=Portable 9=Laptop 10=Notebook 11=Handheld 14=SubNotebook
# 30=Tablet 31=Convertible 32=Detachable
LAPTOP_CHASSIS_TYPES = frozenset((8, 9, 10, 11, 14, 30, 31, 32))

# 虚拟机型号关键词：有电池也不能当笔记本（云主机/UPS 场景）
VM_MODEL_KEYWORDS = ("VIRTUAL", "VMWARE", "HVM", "KVM", "QEMU", "XEN", "HYPER-V")


@dataclass
class HardwareInfo:
    cpu_name: str = "未知 CPU"
    cpu_cores: int = 0
    cpu_threads: int = 0
    cpu_base_mhz: int = 0
    gpu_name: str = "未知显卡"
    gpu_vram_bytes: int = 0
    gpu_resolution: str = ""
    ram_bytes: int = 0
    disks: list = field(default_factory=list)          # [(media_type, size_gb), ...]
    monitor_count: int = 1
    has_battery: bool = False
    # v18.31：以机箱类型(ChassisTypes)判定的便携机标志，比单看电池可靠
    # （接 UPS 的台式机 Win32_Battery 也会命中）
    is_laptop: bool = False
    # v18.31：每台显示器的 EDID 物理尺寸，[{"label","pnp","w_cm","h_cm","inches"}, ...]
    # 用于按面积连续估算显示器功耗；取不到 EDID 时为空列表，功耗模型回退到分辨率分档
    monitors: list = field(default_factory=list)
    gpu_is_nvidia: bool = False
    os_caption: str = ""
    raw: dict = field(default_factory=dict)


def _decode(raw: bytes) -> str:
    """PowerShell 输出可能是 UTF-8 或系统默认编码(GBK)，统一安全解码。"""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _ps(script: str, timeout: int = 15) -> Optional[str]:
    """运行一段 PowerShell 并返回文本输出；失败返回 None。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        if proc.returncode != 0:
            return None
        return _decode(proc.stdout or b"")
    except Exception:
        return None


# ---------------- v18.36 传感器快照（温度 + 风扇转速）----------------
# Windows 免驱拿不到 CPU / 内存 / 主板温度和风扇转速。实测（管理员权限）：
#   Win32_Fan                                             → 空
#   Win32_TemperatureProbe                                → 空
#   MSAcpi_ThermalZoneTemperature (root/WMI)              → 空
#   Win32_PerfFormattedData_Counters_ThermalZoneInformation → 空
#   厂商命名空间 (ASUS/Gigabyte/MSI/ASRock/HP/Dell)        → 无 Sensor 类
# 这些都要主板在 ACPI/SMBIOS 里上报热区，台式机普遍没有。这也是 v18.42 那版
# 「驱动无关」方案失败的根源——它最终只剩 NVIDIA GPU 和少数磁盘温度可读。
# v18.43 起内置 LibreHardwareMonitor：自带 Ring-0 驱动直读 Super I/O / EC，
# 温度、风扇、NVMe 全覆盖，经 http://127.0.0.1:8085/data.json 取数
# （拉起逻辑见下方 ensure_lhm）。
# 取一次有网络往返，故 10s 缓存节流；取不到时冷却 120s 再试。
_SENSOR_TTL_OK = 10.0
_SENSOR_TTL_FAIL = 120.0
_SENSOR_NS = ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor")
_sensor_state = {"last_ts": 0.0, "ttl": 0.0, "fans": [], "temps": {}, "ready": False}
# v18.46 最近一次采样里每条风扇的归属（与 fans 同序）：cpu / board / gpu / ram / other
_FAN_KINDS: list = []

# LHM 传感器名 → 语义键。同一键内关键词按优先级排序：
# 例如 CPU Package 优先于 CPU Core #1（后者是单核温度，不代表整体）。
_TEMP_RULES = (
    ("cpu",         ("cpu package", "cpu total", "cpu cores", "cpu core", "cpu")),
    ("memory",      ("memory", "dimm", "dram", "ram")),
    ("motherboard", ("motherboard", "mainboard", "system", "chipset",
                     "pch", "vrm", "tempin", "aux")),
)


def _parse_sensors(out: str):
    """解析 Sensor 的 ConvertTo-Json 输出为 (fans, temps)。

    fans  = [(name, rpm), ...]
    temps = {"cpu"|"memory"|"motherboard": 摄氏度}
    """
    try:
        data = json.loads(out)
    except Exception:
        return [], {}
    if isinstance(data, dict):
        data = [data]
    fans = []
    cands = {"cpu": [], "memory": [], "motherboard": []}
    for d in data or []:
        if not isinstance(d, dict):
            continue
        try:
            v = float(d.get("Value"))
        except (TypeError, ValueError):
            continue
        st = str(d.get("SensorType") or "")
        name = str(d.get("Name") or "")
        if st == "Fan":
            if v > 0:
                fans.append((name or "Fan", int(round(v))))
            continue
        if st != "Temperature" or v <= 0:
            continue
        low = name.lower()
        for key, kws in _TEMP_RULES:
            for prio, kw in enumerate(kws):
                if kw in low:
                    cands[key].append((prio, v))
                    break
            else:
                continue
            break
    temps = {}
    for key, lst in cands.items():
        if lst:
            lst.sort(key=lambda x: x[0])      # 优先级最小者胜
            temps[key] = round(lst[0][1], 1)
    return fans, temps


def _parse_lhm_json(text: str):
    """解析 LibreHardwareMonitor Web Server 的 /data.json 树。

    节点结构：{Text, Min, Value, Max, Type, HardwareId, Children}；
    Value 是带单位的字符串（如 "79.6 °C"、"1937 RPM"），取首段转 float。
    温度分类靠节点 HardwareId 前缀：
      /amdcpu /intelcpu → cpu；/lpc /motherboard → 主板；/ram → 内存。
    SuperIO 坏通道（实测 110/106/103° 的 AUX 电压等效读数）用 5–100°C 过滤。

    v18.46 风扇归属：fans 仍是 [(名称, RPM)]（兼容旧调用），另按同序记录每条
    风扇所属硬件（cpu / board / gpu / ram / other）到模块级 _FAN_KINDS，
    供 UI 单独取「显卡风扇转速」。

    返回 (fans, temps, ready)。
    """
    try:
        data = json.loads(text)
    except Exception:
        return [], {}, False
    fans = []
    fan_kinds = []
    cands = {"cpu": [], "memory": [], "motherboard": [], "gpu": []}

    def _val(node):
        try:
            return float(str(node.get("Value") or "").split()[0])
        except (ValueError, IndexError):
            return None

    def walk(node, kind):
        hwid = str(node.get("HardwareId") or "")
        if hwid.startswith(("/amdcpu", "/intelcpu")):
            kind = "cpu"
        elif hwid.startswith(("/lpc", "/motherboard")):
            kind = "board"
        elif hwid.startswith("/ram"):
            kind = "ram"
        elif hwid.startswith("/gpu"):
            kind = "gpu"
        tp = node.get("Type") or ""
        if tp in ("Temperature", "Fan"):
            v = _val(node)
            if v is not None:
                name = str(node.get("Text") or "")
                if tp == "Fan":
                    if v > 0:
                        fans.append((name or "Fan", int(round(v))))
                        # v18.46 归属：显卡节点下的是显卡风扇（/gpu-nvidia/0 等）
                        fan_kinds.append(kind if kind in ("cpu", "board", "gpu", "ram")
                                         else "other")
                elif 5.0 <= v <= 100.0:          # 过滤坏通道
                    low = name.lower()
                    if kind == "cpu":
                        # 优先级：Tctl/Tdie 整体温度 > Core > CCD 单核
                        # （"CCD1 (Tdie)" 也含 tdie，不能据此给最高优先级）
                        if "tctl" in low:
                            prio = 0
                        elif "core" in low:
                            prio = 1
                        elif "ccd" in low:
                            prio = 2
                        else:
                            prio = 3
                        cands["cpu"].append((prio, v))
                    elif kind == "gpu":
                        # 与 LHM 面板一致：主温度取 GPU Core，其次 Hot Spot
                        gl = name.lower()
                        prio = 0 if "core" in gl else (1 if "hot" in gl else 2)
                        cands["gpu"].append((prio, v))
                    elif kind == "board":
                        cands["motherboard"].append((0, v))
                    elif kind == "ram":
                        cands["memory"].append((0, v))
        for c in node.get("Children") or []:
            walk(c, kind)

    walk(data, "")
    temps = {}
    for key, lst in cands.items():
        if lst:
            lst.sort(key=lambda x: x[0])
            temps[key] = round(lst[0][1], 1)
    # v18.46 风扇归属缓存（与 fans 同序）
    try:
        _FAN_KINDS[:] = list(fan_kinds)
    except Exception:
        pass
    # v18.37 同时缓存完整传感器树（供 UI 按 LHM 的分组/命名展示原始读数）
    try:
        _LHM_SENS.update(groups=_build_lhm_tree(data), ts=time.time(), ok=True)
    except Exception:
        pass
    return fans, temps, True


def fan_kinds_cached() -> list:
    """与 fan_rpms_cached() 同序的风扇归属列表（长度可能小于 fans，取不到时按 other）。"""
    fan_rpms_cached()
    return list(_FAN_KINDS)


def fans_by_kind(kind: str) -> list:
    """v18.46 按归属取风扇 [(名称, RPM)]：kind = gpu | board | cpu | ram | other。

    显卡风扇 kind='gpu'（LHM 硬件节点 /gpu-nvidia/0、/gpu-amd/0 下的 Fan）。
    """
    try:
        fan_rpms_cached()
        return [(n, v) for (n, v), k in zip(fan_rpms_cached(), _FAN_KINDS)
                if k == kind]
    except Exception:
        return []


def gpu_fan_rpms() -> list:
    """v18.46 显卡风扇转速 [(名称, RPM)]；无独显风扇时为空列表。"""
    return fans_by_kind("gpu")


# ---------------------------------------------------------------------------
# v18.37 LHM 完整传感器快照 —— 让 UI 能「以 LibreHardwareMonitor 为参考」显示
# 温度/转速：分组、命名、Min/Value/Max 全部照搬 LHM 的 Web Server 输出。
# ---------------------------------------------------------------------------
_LHM_SENS = {"ts": 0.0, "ok": False, "groups": []}


def _build_lhm_tree(root) -> list:
    """把 LHM /data.json 还原成与它界面一致的「硬件 → 传感器」两级结构。

    硬件节点  = 带 HardwareId（如 /amdcpu/0、/lpc/nct6793d/0、/gpu-nvidia/0）
    传感器节点 = 带 SensorId（Text/Min/Value/Max 均带单位，原样保留）
    """
    groups = []

    def visit(node, cur, parent=""):
        hwid = node.get("HardwareId")
        sid = node.get("SensorId")
        if hwid:
            cur = {"name": str(node.get("Text") or ""), "hwid": str(hwid),
                   "parent": parent, "sensors": []}
            groups.append(cur)
            parent = str(node.get("Text") or "")
        elif cur is not None and (sid or node.get("Type")):
            # 真实 LHM 有 SensorId；单元测试的简化样例只有 Type，两者都要认
            cur["sensors"].append({
                "name": str(node.get("Text") or ""),
                "type": str(node.get("Type") or ""),
                "min": str(node.get("Min") or ""),
                "value": str(node.get("Value") or ""),
                "max": str(node.get("Max") or ""),
                "sid": str(sid),
            })
        for c in node.get("Children") or []:
            visit(c, cur, parent)

    visit(root, None)
    return groups


def lhm_sensors(force: bool = False) -> dict:
    """返回 {"ok", "ts", "groups"}：与 LHM 面板同构的全部传感器（含 Min/Max）。

    force=True 会立即重新拉一次 /data.json（普通采样有 10s 缓存）。
    """
    if force or not _LHM_SENS.get("groups"):
        sensor_snapshot_cached(force=True)
    return dict(_LHM_SENS)


# 硬件 HardwareId 前缀 → 部件类别
_LHM_KIND_PREFIX = (
    ("/amdcpu", "cpu"), ("/intelcpu", "cpu"),
    ("/lpc", "motherboard"), ("/motherboard", "motherboard"),
    ("/ram", "memory"), ("/memory", "memory"),   # DIMM 是 /memory/dimm/N
    ("/gpu", "gpu"),
    ("/nvme", "disk"), ("/hdd", "disk"),
)


def lhm_gpu_load():
    """LHM 的 GPU Core 占用率（%）：非 N 卡或 nvidia-smi 不可用时的真实占用来源。

    与任务管理器「GPU 利用率」同一口径（LHM 对 N 卡读 NVML、A 卡读 ADL/GPU 节点）。
    """
    for g in _LHM_SENS.get("groups") or []:
        if not (g.get("hwid") or "").startswith("/gpu"):
            continue
        for sn in g.get("sensors") or []:
            if sn.get("type") != "Load":
                continue
            low = (sn.get("name") or "").lower()
            if low == "gpu core" or (low.startswith("gpu core") and "memory" not in low):
                try:
                    v = float((sn.get("value") or "").split()[0])
                except (ValueError, IndexError):
                    return None
                return max(0.0, min(100.0, v))
    return None


def lhm_probe(kind: str):
    """取 LHM 中某类部件的全部温度/转速读数（只读缓存，不触发网络）。

    kind: cpu | motherboard | memory | gpu | disk | fan
      fan → 所有当前转速 > 0 的风扇（含显卡风扇）
      其余 → 对应硬件分组下的 Temperature（5–100°C 过滤 SuperIO 坏通道）
    返回 [("硬件名 · 传感器名", "原始值文本"), ...]，顺序与 LHM 面板一致。
    """
    out = []
    for g in _LHM_SENS.get("groups") or []:
        hwid = g.get("hwid") or ""
        kind_of = ""
        for pfx, k in _LHM_KIND_PREFIX:
            if hwid.startswith(pfx):
                kind_of = k
                break
        for sn in g.get("sensors") or []:
            tp = sn.get("type")
            try:
                v = float((sn.get("value") or "").split()[0])
            except (ValueError, IndexError):
                continue
            if kind == "fan":
                if tp != "Fan" or v <= 0:
                    continue
            else:
                if tp != "Temperature" or kind_of != kind:
                    continue
                if not (5.0 <= v <= 100.0):
                    continue
                _n = (sn.get("name") or "").lower()
                if "warning" in _n or "critical" in _n:
                    continue   # NVMe 的告警/临界阈值，不是实测温度
            out.append((f"{g.get('name')} · {sn.get('name')}",
                        sn.get("value") or ""))
    return out


# ---------------------------------------------------------------------------
# v18.43 温度/风扇读取 —— 统一回到 LibreHardwareMonitor
#
# v18.42 曾用「驱动无关」方案（WMI + nvidia-smi），实测覆盖极窄：CPU / 主板 /
# 内存 / NVMe / 风扇转速在 WMI 里全部读不到，只有 NVIDIA GPU 和少量磁盘温度，
# 因此 v18.43 改回以内置 LHM 作为唯一温度源。
#
# 数据流：内置 lhm_bin/LibreHardwareMonitor.exe（隐藏运行）
#         → http://127.0.0.1:8085/data.json（LHM 树结构）
#         → _parse_lhm_json 解析出 temps/fans + 完整传感器分组
#
# 关键坑（本次修复的根因）：LHM 读的配置文件是
#     Path.ChangeExtension(Application.ExecutablePath, ".config")
#   → LibreHardwareMonitor.config，而**不是** LibreHardwareMonitor.exe.config。
#   前者是它自己的 PersistentSettings（写 runWebServerMenuItem=true 才生效），
#   后者只服务于 CLR 程序集绑定。写错文件会导致 8085 永不监听且无任何报错。
# ---------------------------------------------------------------------------
_LHM_PORT = 8085
_LHM_URL = "http://127.0.0.1:%d/data.json" % _LHM_PORT
_LHM_EXE = "LibreHardwareMonitor.exe"
# 冷启动后 Web Server 响应了，但 HDD / NVMe 这类慢热传感器的 Value 还要
# 再过约 0.5s 才填上（实测：到位瞬间 disk 读数只有 3/4，+0.5s 才齐）。
# 等这一小会儿，让交给 UI 的首帧就是完整的，不必等下一个 10s 周期。
_LHM_WARMUP = 1.5
_lhm_state = {"retry_after": 0.0, "launched_ts": 0.0}


def _lhm_bin_dir() -> str:
    """定位内置 LHM 目录：PyInstaller 走 _MEIPASS，源码直跑走脚本旁。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cand = os.path.join(base, "lhm_bin")
        if os.path.isdir(cand):
            return cand
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "lhm_bin")


def _lhm_fetch(timeout: float = 2.5):
    """取 /data.json 原文；LHM 未运行 / 超时返回 None。

    显式声明 Accept-Encoding: identity 绕开 gzip——LHM 认 Accept-Encoding
    含 gzip 时会回压缩体，这里直接要明文，少一道解压环节也更好排错。
    """
    try:
        req = urllib.request.Request(_LHM_URL,
                                     headers={"Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def is_lhm_running() -> bool:
    """Web Server 是否可用（用真实取数判定，比扫端口可靠）。"""
    return _lhm_fetch(timeout=1.5) is not None


def ensure_lhm(wait: float = 12.0) -> bool:
    """确保内置 LHM 在后台运行且 Web Server 就绪；必要时隐藏拉起。

    用 STARTF_USESHOWWINDOW + SW_HIDE 配合 CREATE_NO_WINDOW 启动，进程无窗口、
    无控制台闪现。已运行时直接返回；拉起失败进入冷却期，避免每周期反复起进程。
    返回 True 表示此刻可以取到 /data.json。
    """
    now = time.time()
    if is_lhm_running():
        _lhm_state["retry_after"] = 0.0
        return True
    if now < _lhm_state["retry_after"]:
        return False

    exe = os.path.join(_lhm_bin_dir(), _LHM_EXE)
    if not os.path.isfile(exe):
        _lhm_state["retry_after"] = now + 180.0
        return False

    try:
        si = subprocess.STARTUPINFO()          # 非 Windows 上不存在，抛错由下面兜住
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                     # SW_HIDE
        cf = 0x08000000                        # CREATE_NO_WINDOW
        if sys.platform == "win32":
            cf |= 0x00000008                   # DETACHED_PROCESS
        subprocess.Popen(
            [exe], cwd=os.path.dirname(exe), startupinfo=si, creationflags=cf,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        _lhm_state["retry_after"] = now + 60.0
        return False

    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.8)
        if is_lhm_running():
            time.sleep(_LHM_WARMUP)         # 等慢热传感器（HDD/NVMe）填上首帧值
            _lhm_state["launched_ts"] = time.time()
            _lhm_state["retry_after"] = 0.0
            return True
    _lhm_state["retry_after"] = time.time() + 45.0
    return False


def sensor_snapshot_cached(force: bool = False):
    """(fans, temps, ready)：10s 缓存；force 用于「重新检测硬件」立即刷新。

    v18.43 起数据源回到内置 LibreHardwareMonitor：CPU(Tctl/Tdie)、GPU Core /
    Hot Spot、主板 SuperIO、内存、NVMe、机械盘、风扇转速全覆盖。SuperIO 上未接
    探头的脏通道（实测 3°C / 110°C）由 _parse_lhm_json 的 5–100°C 过滤剔除。
    LHM 尚未运行时会顺手隐藏拉起；确实拉不起来才 ready=False 并转入长冷却。
    """
    now = time.time()
    st = _sensor_state
    if not force and now - st["last_ts"] < st["ttl"]:
        return st["fans"], st["temps"], st["ready"]

    text = _lhm_fetch()
    if text is None:
        ensure_lhm()
        text = _lhm_fetch()
    if text is None:
        st.update(last_ts=now, ttl=_SENSOR_TTL_FAIL, ready=False)
        return st["fans"], st["temps"], False

    fans, temps, ready = _parse_lhm_json(text)
    st.update(fans=fans, temps=temps, ready=ready, last_ts=now,
              ttl=_SENSOR_TTL_OK if ready else _SENSOR_TTL_FAIL)
    try:
        st["fan_kinds"] = list(_FAN_KINDS)
    except Exception:
        pass
    return fans, temps, ready



def fan_rpms_cached() -> list:
    """向后兼容：v18.35 起的接口，只取风扇部分。"""
    fans, _t, _r = sensor_snapshot_cached()
    return fans


def _is_virtual_gpu(name: str) -> bool:
    """判断显示适配器名称是否为虚拟/伪显卡（应被排除，不能当主显卡）。

    原实现只比对 Virtual/Basic/Microsoft 三个词，实测漏掉：
      · GameViewer Display Adapter —— 远控/串流软件装的虚拟显卡（本机存在）
      · Idd Desk Adapter          —— Windows 间接显示驱动
      · DDA Wrapper               —— 显卡直通
    这些一旦被当成主显卡，identify_gpu 会返回 75W/low，真实 180W 显卡被低估 58%。
    """
    up = (name or "").upper()
    return any(k in up for k in VIRTUAL_GPU_KEYWORDS)


def _extract_monitor_pnp(device_id: str) -> str:
    """从 PnP 设备 ID 提取显示器型号码。

    'DISPLAY\\SGT2450\\5&264d596b&0&UID41222_0' → 'SGT2450'
    '#'(设备路径分隔符) 先归一化为 '\\'，再取 DISPLAY/MONITOR 的下一段。
    """
    parts = (device_id or "").replace("#", "\\").split("\\")
    parts = [p for p in parts if p]
    for i, p in enumerate(parts[:-1]):
        if p.upper() in ("DISPLAY", "MONITOR"):
            return parts[i + 1]
    # 兜底：取首个以三个字母开头的段（厂商码格式）
    for p in parts:
        if len(p) >= 3 and p[:3].isalpha():
            return p
    return ""


def _detect_laptop(chassis_types, model: str, has_battery: bool) -> bool:
    """便携机判定：机箱类型为准，电池仅作兜底，虚拟机一律排除。

    旧实现只看 Win32_Battery 计数 —— 台式机接 UPS 时同样会命中
    （UPS 经 USB HID 暴露电池，Win32_Battery 能枚举到），于是整机基线功耗
    按笔记本算，误差可观。ChassisTypes 来自 SMBIOS，是权威的形态依据。
    """
    if chassis_types:
        return any(t in LAPTOP_CHASSIS_TYPES for t in chassis_types)
    up = (model or "").upper()
    if any(k in up for k in VM_MODEL_KEYWORDS):
        return False
    return bool(has_battery)


def _diag_inches(w_cm: float, h_cm: float) -> Optional[float]:
    """EDID 物理宽高(cm) → 对角线英寸。"""
    if w_cm > 0 and h_cm > 0:
        return round(((w_cm ** 2 + h_cm ** 2) ** 0.5) / 2.54, 1)
    return None


def _monitor_edid_sizes() -> dict:
    """读显示器 EDID 物理尺寸：root\\WMI WmiMonitorBasicDisplayParams。

    返回 {PNP码大写: (宽cm, 高cm)}。物理显示器普遍有值，
    虚拟机 / 纯远程桌面会话通常为空——此时功耗模型回退到分辨率分档。
    """
    out = _ps(
        "(Get-CimInstance -Namespace root\\WMI -ClassName WmiMonitorBasicDisplayParams | "
        "Select-Object InstanceName,MaxHorizontalImageSize,MaxVerticalImageSize | ConvertTo-Json)"
    )
    sizes: dict = {}
    if not out:
        return sizes
    try:
        arr = json.loads(out)
        if isinstance(arr, dict):
            arr = [arr]
        for it in arr:
            pnp = _extract_monitor_pnp(it.get("InstanceName") or "")
            if not pnp:
                continue
            try:
                w = int(it.get("MaxHorizontalImageSize") or 0)
                h = int(it.get("MaxVerticalImageSize") or 0)
            except Exception:
                continue
            if w > 0 and h > 0:
                sizes[pnp.upper()] = (w, h)
    except Exception:
        pass
    return sizes


def detect_hardware() -> HardwareInfo:
    info = HardwareInfo()

    # CPU
    out = _ps(
        "(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,"
        "NumberOfLogicalProcessors,MaxClockSpeed | ConvertTo-Json)"
    )
    if out:
        try:
            d = json.loads(out)
            if isinstance(d, list):
                d = d[0]
            info.cpu_name = (d.get("Name") or "未知 CPU").strip()
            info.cpu_cores = int(d.get("NumberOfCores") or 0)
            info.cpu_threads = int(d.get("NumberOfLogicalProcessors") or 0)
            info.cpu_base_mhz = int(d.get("MaxClockSpeed") or 0)
        except Exception:
            pass

    # GPU（取第一个带名称的显示适配器；记录是否为 NVIDIA）
    out = _ps(
        "(Get-CimInstance Win32_VideoController | Where-Object {$_.Name} | "
        "Select-Object Name,AdapterRAM,CurrentHorizontalResolution,CurrentVerticalResolution | ConvertTo-Json)"
    )
    if out:
        try:
            arr = json.loads(out)
            if isinstance(arr, dict):
                arr = [arr]
            for g in arr:
                name = (g.get("Name") or "").strip()
                if not name:
                    continue
                # v18.31 跳过虚拟/伪显卡：黑名单见 VIRTUAL_GPU_KEYWORDS
                if _is_virtual_gpu(name):
                    continue
                info.gpu_name = name
                info.gpu_vram_bytes = int(g.get("AdapterRAM") or 0)
                h = g.get("CurrentHorizontalResolution") or 0
                v = g.get("CurrentVerticalResolution") or 0
                if h and v:
                    info.gpu_resolution = f"{h}x{v}"
                info.gpu_is_nvidia = "NVIDIA" in name.upper()
                break
        except Exception:
            pass

    # RAM
    out = _ps("(Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory | ConvertTo-Json)")
    if out:
        try:
            d = json.loads(out)
            info.ram_bytes = int(d.get("TotalPhysicalMemory") or 0)
        except Exception:
            pass

    # 磁盘
    out = _ps(
        "(Get-PhysicalDisk | Select-Object MediaType,@{N='SizeGB';E={[math]::Round($_.Size/1GB,1)}} | ConvertTo-Json)"
    )
    if out:
        try:
            arr = json.loads(out)
            if isinstance(arr, dict):
                arr = [arr]
            for d in arr:
                info.disks.append((d.get("MediaType") or "未知", float(d.get("SizeGB") or 0)))
        except Exception:
            pass

    # v18.31 显示器：取名称 + PnP 设备 ID，再与 EDID 物理尺寸关联，得到每台英寸数
    out = _ps(
        "(Get-CimInstance Win32_PnPEntity -Filter \"PNPClass='Monitor'\" | "
        "Where-Object {$_.Name} | Select-Object Name,PNPDeviceID | ConvertTo-Json)"
    )
    edid = _monitor_edid_sizes()
    monitors = []
    if out:
        try:
            arr = json.loads(out)
            if isinstance(arr, dict):
                arr = [arr]
            for m in arr:
                pnp = _extract_monitor_pnp(m.get("PNPDeviceID") or "")
                size = edid.get(pnp.upper()) if pnp else None
                # 单显示器时 PnP 码可能因 UID 后缀对不上，直接取唯一一条 EDID
                if not size and len(arr) == 1 and len(edid) == 1:
                    size = next(iter(edid.values()), None)
                w_cm, h_cm = size or (0, 0)
                monitors.append({
                    "label": (m.get("Name") or "").strip(),
                    "pnp": pnp,
                    "w_cm": w_cm,
                    "h_cm": h_cm,
                    "inches": _diag_inches(w_cm, h_cm),
                })
        except Exception:
            monitors = []
    info.monitors = monitors
    info.monitor_count = max(1, len(monitors)) if monitors else 1

    # v18.31 便携机判定：机箱类型为准，电池仅兜底
    # （旧实现只看 Win32_Battery，台式机接 UPS 会被误判成笔记本）
    chassis_raw = _ps(
        "(Get-CimInstance Win32_SystemEnclosure | Select-Object -ExpandProperty ChassisTypes)")
    chassis = set()
    for tok in re.findall(r"\d+", chassis_raw or ""):
        try:
            chassis.add(int(tok))
        except Exception:
            pass
    model = (_ps("(Get-CimInstance Win32_ComputerSystem | Select-Object -ExpandProperty Model)")
             or "").strip()
    bat_out = _ps("(Get-CimInstance Win32_Battery | Measure-Object | Select-Object -ExpandProperty Count)")
    has_battery = bool(bat_out and bat_out.strip().isdigit() and int(bat_out.strip()) > 0)
    info.has_battery = has_battery
    info.is_laptop = _detect_laptop(chassis, model, has_battery)

    # 操作系统
    out = _ps("(Get-CimInstance Win32_OperatingSystem | Select-Object -ExpandProperty Caption)")
    if out:
        info.os_caption = out.strip()

    info.raw = {
        "cpu": info.cpu_name, "gpu": info.gpu_name,
        "ram_gb": round(info.ram_bytes / 1e9, 1),
        "disks": info.disks, "monitors": info.monitor_count,
        "is_laptop": info.is_laptop,
        "monitor_inches": [m.get("inches") for m in info.monitors],
    }
    return info


def sample_load() -> dict:
    """实时采样：返回 {'cpu_load': 0-100, 'gpu_power': float|None, 'gpu_valid': bool}。"""
    script = r'''
$result = @{ cpu = 0; gpuPower = $null; gpuValid = $false }
try {
    $c = (Get-Counter '\Processor(_Total)\% Processor Time' -ErrorAction Stop).CounterSamples.CookedValue
    $result.cpu = [math]::Round($c, 1)
} catch { $result.cpu = 0 }

if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    try {
        $p = & nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits 2>$null
        if ($p) {
            $v = [double]($p.ToString().Trim().Split([Environment]::NewLine)[0])
            $result.gpuPower = [math]::Round($v, 1)
            $result.gpuValid = $true
        }
    } catch { $result.gpuValid = $false }
}
$result | ConvertTo-Json -Compress
'''
    out = _ps(script, timeout=12)
    if not out:
        return {"cpu_load": 0.0, "gpu_power": None, "gpu_valid": False}
    try:
        d = json.loads(out)
        return {
            "cpu_load": float(d.get("cpu") or 0.0),
            "gpu_power": float(d["gpuPower"]) if d.get("gpuPower") is not None else None,
            "gpu_valid": bool(d.get("gpuValid")),
        }
    except Exception:
        return {"cpu_load": 0.0, "gpu_power": None, "gpu_valid": False}


def collect_system_info() -> dict:
    """一次性静态系统信息（AIDA64 风格侧栏）。任一环节失败对应键留空，不抛错。"""
    out = _ps(r'''
$r = @{}
try {
  $os = Get-CimInstance Win32_OperatingSystem
  $cs = Get-CimInstance Win32_ComputerSystem
  $bb = Get-CimInstance Win32_BaseBoard
  $bi = Get-CimInstance Win32_BIOS
  $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
  $pma = Get-CimInstance Win32_PhysicalMemoryArray | Select-Object -First 1
  $mods = Get-CimInstance Win32_PhysicalMemory
  $gpu = Get-CimInstance Win32_VideoController | Where-Object {$_.Name -and $_.Name -notmatch 'Virtual|Basic|Microsoft'} | Select-Object -First 1
  $fw = $null; $sb = $null
  try { $fw = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control' -Name PEFirmwareType -ErrorAction Stop).PEFirmwareType } catch {}
  try { $sb = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\SecureBoot' -Name UEFISecureBootEnabled -ErrorAction Stop).UEFISecureBootEnabled } catch {}
  $tpm = ''
  try { $t = Get-CimInstance -Namespace root\cimv2\Security\MicrosoftTpm -ClassName Win32_Tpm -ErrorAction Stop; if ($t) { $tpm = ($t.SpecVersion -split ',')[0] } } catch {}
  $l1 = 0; $l2 = 0; $l3 = 0
  try {
    $cm = Get-CimInstance Win32_CacheMemory
    $l1 = ($cm | Where-Object {$_.Level -eq 3} | Measure-Object InstalledSize -Sum).Sum
    $l2 = ($cm | Where-Object {$_.Level -eq 4} | Measure-Object InstalledSize -Sum).Sum
    $l3 = ($cm | Where-Object {$_.Level -eq 5} | Measure-Object InstalledSize -Sum).Sum
  } catch {}
  $net = Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=true' | Select-Object -First 1
  $r.os = $os.Caption; $r.osver = $os.BuildNumber; $r.osarch = $os.OSArchitecture
  $r.comp = $cs.Name; $r.domain = $cs.Domain
  $r.mb = ('{0} {1}' -f $bb.Manufacturer, $bb.Product).Trim()
  $r.bios = '{0}  Date: {1}' -f $bi.SMBIOSBIOSVersion, $bi.ReleaseDate
  $r.cpu = $cpu.Name; $r.cores = $cpu.NumberOfCores; $r.threads = $cpu.NumberOfLogicalProcessors
  $r.mhz = $cpu.MaxClockSpeed; $r.l1 = $l1; $r.l2 = $l2; $r.l3 = $l3
  $r.gpuname = $gpu.Name; $r.gpuvram = $gpu.AdapterRAM
  $r.gpures = '{0}x{1}' -f $gpu.CurrentHorizontalResolution, $gpu.CurrentVerticalResolution
  $r.gpuref = $gpu.CurrentRefreshRate
  $r.slots = $pma.MemoryDevices; $r.maxcap = $pma.MaxCapacityEx
  $r.mods = @($mods | ForEach-Object {
    [pscustomobject]@{ m = $_.Manufacturer; pn = ('' + $_.PartNumber).Trim();
      spd = $_.Speed; cap = $_.Capacity; clk = $_.ConfiguredClockSpeed } })
  $r.fw = $fw; $r.sb = $sb; $r.tpm = $tpm
  if ($net) {
    $r.nic = $net.Description; $r.mac = $net.MACAddress
    $r.ip = ($net.IPAddress | Where-Object { $_ -match '\.' } | Select-Object -First 1)
    $r.gw = ($net.DefaultIPGateway | Select-Object -First 1)
  }
} catch {}
$r | ConvertTo-Json -Depth 3
''', timeout=25)
    d = {}
    if out:
        try:
            d = json.loads(out)
        except Exception:
            d = {}
    if not d.get("fw"):
        # 注册表回退：PEFirmwareType 1=BIOS 2=UEFI
        out = _ps("reg query 'HKLM\\SYSTEM\\CurrentControlSet\\Control' /v PEFirmwareType", timeout=6)
        if out and "PEFirmwareType" in out:
            m = re.search(r"REG_DWORD\s+0x([0-9a-fA-F]+)", out)
            if m:
                d["fw"] = {"1": "Legacy BIOS", "2": "UEFI"}.get(m.group(1).lower(), "")
    if not d.get("sb") and d.get("fw") == "UEFI":
        out = _ps("reg query 'HKLM\\SYSTEM\\CurrentControlSet\\Control\\SecureBoot' /v UEFISecureBootEnabled", timeout=6)
        if out and "0x1" in out:
            d["sb"] = 1
    if isinstance(d.get("mods"), dict):
        d["mods"] = [d["mods"]]
    # 磁盘（含分区盘符）
    out = _ps(r'''
$disks = @()
try {
  $disks = Get-PhysicalDisk | Sort-Object DeviceId | ForEach-Object {
    $d = $_
    $letters = ''
    try {
      $ls = ($d | Get-Disk | Get-Partition | Where-Object DriveLetter |
        ForEach-Object { '{0}:' -f $_.DriveLetter })
      $letters = ($ls -join ' ')
    } catch {}
    [pscustomobject]@{ model = $d.FriendlyName; media = $d.MediaType; bus = $d.BusType;
      sizeGB = [math]::Round($d.Size / 1GB, 0); letters = $letters }
  }
} catch {}
$disks | ConvertTo-Json -Depth 3
''', timeout=20)
    if out:
        try:
            arr = json.loads(out)
            if isinstance(arr, dict):
                arr = [arr]
            d["disks"] = arr
        except Exception:
            d["disks"] = []
    else:
        d["disks"] = []
    return d


def _cpu_temp_once():
    """CPU 温度（热区近似，非所有主板可用）；失败返回 None。"""
    out = _ps("(Get-CimInstance Win32_PerfFormattedData_Counters_ThermalZoneInformation "
              "| Select-Object -First 1).Temperature", timeout=6)
    if not out:
        return None
    try:
        v = float(out.strip())
        # 多数驱动以 1/10 开尔文上报；>1000 视为该格式，换算成摄氏度
        if v > 1000:
            v = v / 10.0 - 273.15
        if -20 < v < 150:
            return round(v, 1)
    except Exception:
        pass
    return None


def _disk_temps_once() -> dict:
    """各物理磁盘温度（StorageReliabilityCounter；非管理员可能拿不到，返回空表）。"""
    out = _ps(r'''
$out = @()
try {
  $out = Get-PhysicalDisk | ForEach-Object {
    $d = $_; $t = $null
    try { $t = ($d | Get-StorageReliabilityCounter -ErrorAction Stop).Temperature } catch {}
    if ($t) { [pscustomobject]@{ m = $d.FriendlyName; t = [double]$t } }
  }
} catch {}
$out | ConvertTo-Json -Depth 2
''', timeout=10)
    d = {}
    if out:
        try:
            arr = json.loads(out)
            if isinstance(arr, dict):
                arr = [arr]
            for x in arr:
                try:
                    d[str(x.get("m"))] = float(x.get("t"))
                except Exception:
                    continue
        except Exception:
            pass
    return d


def sample_dynamic(prev_net):
    """轻量动态信息（psutil 进程内，微秒级）：内存/CPU频率/网速/开机时间。
    prev_net: 上次 (ts, sent, recv) 或 None。返回 (info, cur_net)。"""
    info = {}
    cur = None
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            info["ram_total"] = vm.total
            info["ram_used"] = vm.total - vm.available
            info["ram_pct"] = vm.percent
        except Exception:
            pass
        try:
            f = psutil.cpu_freq()
            if f:
                info["mhz"] = int(f.current)
        except Exception:
            pass
        try:
            info["boot"] = psutil.boot_time()
        except Exception:
            pass
        try:
            io = psutil.net_io_counters()
            cur = (time.time(), io.bytes_sent, io.bytes_recv)
            if prev_net and cur[0] > prev_net[0]:
                dt = cur[0] - prev_net[0]
                info["down_kbs"] = max(0.0, (cur[2] - prev_net[2]) / dt / 1024.0)
                info["up_kbs"] = max(0.0, (cur[1] - prev_net[1]) / dt / 1024.0)
        except Exception:
            pass
    # v18.36 传感器快照（风扇 + CPU/内存/主板温度；内部 10s 缓存节流，
    # worker 线程执行，不卡 UI）。数据源为 LibreHardwareMonitor/OpenHardwareMonitor，
    # 没装则 fans/temps 为空、sensor_ready=False（UI 据此提示）。
    try:
        fans, temps, ready = sensor_snapshot_cached()
        if fans:
            info["fans"] = fans
            # v18.46 风扇归属（与 fans 同序）：供 UI 单列显卡风扇转速
            info["fan_kinds"] = fan_kinds_cached()
        if temps:
            info["sensor_temps"] = temps
        info["sensor_ready"] = ready
    except Exception:
        pass
    return info, cur


class PersistentSampler:
    """常驻采样器：CPU 负载与每进程 CPU 时间用 psutil 在进程内直接读取
    （零子进程、无管道缓冲问题），N 卡功耗用 nvidia-smi -l 常驻流 + 后台读线程。
    旧方案每周期 spawn PowerShell（每次数百 ms CPU）是整机卡顿来源之一。
    GPU 流断/启动失败自动降级；psutil 缺失时 start() 抛错，由调用方回退旧方案。"""

    _STALE_MIN = 8.0   # GPU 最新值的最长可信时长（s）

    def __init__(self, interval_ms: int = 2000, gpu_nvidia: bool = True):
        self.interval_ms = max(1000, int(interval_ms))
        self.gpu_nvidia = bool(gpu_nvidia)
        self._gpu_proc = None
        self._lock = threading.Lock()
        # (功耗W, GPU温度°C, GPU占用率%, valid, ts)
        self._gpu_val = (None, None, None, False, 0.0)
        self._psutil = None
        self._net_prev = None
        self._n_samples = 0
        self._cpu_temp = None
        self._cpu_temp_every = max(10, int(30000 // max(1000, interval_ms)))  # ≈每 30s 测一次
        self._disk_temps = {}
        self._disk_temp_every = max(10, int(60000 // max(1000, interval_ms)))  # ≈每 60s 测一次
        self._dt_busy = False

    def start(self):
        if psutil is None:
            raise RuntimeError("psutil 不可用")
        self._psutil = psutil
        psutil.cpu_percent(interval=None)   # 首调仅建立基线
        if self.gpu_nvidia:
            try:
                self._gpu_proc = subprocess.Popen(
                    # v18.38 增加 utilization.gpu：GPU 使用率取真实 SM 占用，
                    # 不再用「功耗 ÷ TDP」估算（后者空载也有 8~10%，与任务管理器差很远）
                    ["nvidia-smi",
                     "--query-gpu=power.draw,temperature.gpu,utilization.gpu",
                     "--format=csv,noheader,nounits",
                     "-l", str(max(1, self.interval_ms // 1000))],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000)
                threading.Thread(target=self._gpu_reader, daemon=True).start()
            except Exception:
                self._gpu_proc = None

    _SKIP_NAMES = {"system idle process", "system", "idle"}   # 空闲/内核占位，不做分摊展示

    def _collect_apps(self) -> dict:
        """各进程累计 CPU 秒（user+system），按进程名聚合。"""
        m = {}
        for p in self._psutil.process_iter():
            try:
                nm = p.name() or "?"
                if nm.lower() in self._SKIP_NAMES:
                    continue
                t = p.cpu_times()
                m[nm] = m.get(nm, 0.0) + (t.user + t.system)
            except Exception:
                continue
        return m

    def _gpu_reader(self):
        try:
            while True:
                raw = self._gpu_proc.stdout.readline()
                if not raw:
                    break
                s = _decode(raw).strip()
                if not s:
                    continue
                try:
                    parts = s.split(",")
                    v = float(parts[0])
                    temp = float(parts[1]) if len(parts) > 1 else None
                    util = None
                    if len(parts) > 2:
                        try:
                            util = max(0.0, min(100.0, float(parts[2])))
                        except ValueError:
                            util = None
                    with self._lock:
                        self._gpu_val = (v, temp, util, True, time.time())
                except Exception:
                    continue
        except Exception:
            pass

    def _ct_probe(self):
        """后台 CPU 温度探测：成功则更新缓存，失败保持旧值；结束释放忙碌标志。"""
        try:
            t = _cpu_temp_once()
            if t is not None:
                self._cpu_temp = t
        finally:
            self._ct_busy = False

    def _dt_probe(self):
        """后台磁盘温度探测（约 60s 一次）。"""
        try:
            t = _disk_temps_once()
            if t:
                self._disk_temps = t
        finally:
            self._dt_busy = False

    def sample(self) -> dict:
        now = time.time()
        stale = max(3.0 * self.interval_ms / 1000.0, self._STALE_MIN)
        with self._lock:
            gpu_p, gpu_t, gpu_u, _gpu_ok, gpu_ts = self._gpu_val
        gpu_fresh = gpu_ts > 0 and (now - gpu_ts) <= stale
        self._n_samples += 1
        # v18.2/v18.3：温度探测（spawn PS 可达数秒）放独立线程，绝不阻塞 sample()
        if self._n_samples % self._cpu_temp_every == 1 and not getattr(self, "_ct_busy", False):
            self._ct_busy = True
            threading.Thread(target=self._ct_probe, daemon=True).start()
        if self._n_samples % self._disk_temp_every == 1 and not self._dt_busy:
            self._dt_busy = True
            threading.Thread(target=self._dt_probe, daemon=True).start()
        sysinfo, self._net_prev = sample_dynamic(self._net_prev)
        if gpu_fresh and gpu_t is not None:
            sysinfo["gpu_temp"] = gpu_t
        if self._cpu_temp is not None:
            sysinfo["cpu_temp"] = self._cpu_temp
        if self._disk_temps:
            sysinfo["disk_temps"] = dict(self._disk_temps)
        return {
            "cpu_load": float(self._psutil.cpu_percent(interval=None)),
            "gpu_power": gpu_p if (self.gpu_nvidia and gpu_fresh) else None,
            "gpu_util": gpu_u if (self.gpu_nvidia and gpu_fresh) else None,
            "gpu_valid": bool(self.gpu_nvidia and gpu_fresh),
            "apps": (now, self._collect_apps()),
            "sys": sysinfo,
        }

    def stop(self):
        try:
            if self._gpu_proc is not None and self._gpu_proc.poll() is None:
                self._gpu_proc.kill()
        except Exception:
            pass
        self._gpu_proc = None


def cpu_load_quick() -> float:
    """仅取 CPU 负载百分比（用于非 N 卡估算 GPU 负载的近似）。"""
    out = _ps(r"(Get-Counter '\Processor(_Total)\% Processor Time').CounterSamples.CookedValue", timeout=8)
    if out:
        try:
            return float(out.strip())
        except Exception:
            return 0.0
    return 0.0


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}PB"


# ---------------- 显示器开关状态推断 ----------------
# 限制说明：Windows 没有稳定公开的 API 能读取显示器 DPMST 真实状态，
# GetDevicePowerState("\\\\.\\DISPLAY1") 在多数机器上 CreateFile 即失败。
# 因此采用「用户空闲时长 >= 系统熄屏超时」来推断*系统自动熄屏*；
# 用户手动按显示器电源键关屏无法自动感知，由 UI 手动开关覆盖。
_disp_cache = {"timeout": None, "ts": 0.0}
_DISP_TIMEOUT_TTL = 300.0


def display_off_timeout_sec() -> int:
    """系统电源方案「关闭显示器」超时（秒）。失败回退 600s，带 5 分钟缓存。"""
    now = time.time()
    c = _disp_cache
    if c["timeout"] is not None and (now - c["ts"]) < _DISP_TIMEOUT_TTL:
        return c["timeout"]
    val = 600
    try:
        # PyInstaller --windowed 打包下必须带 CREATE_NO_WINDOW，
        # 否则子进程会尝试创建控制台窗口并挂死调用方（采样线程）
        _cf = 0x08000000 if sys.platform == "win32" else 0
        out = subprocess.run(
            ["powercfg", "/query", "SCHEME_CURRENT", "SUB_VIDEO", "VIDEOIDLE"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="ignore", creationflags=_cf)
        secs = re.findall(r"0x([0-9a-fA-F]{8})", out.stdout or "")
        if secs:
            v = int(secs[-1], 16)
            if 0 < v < 86400:
                val = v
    except Exception:
        pass
    c["timeout"] = val
    c["ts"] = now
    return val


def user_idle_sec() -> float:
    """用户键鼠空闲秒数（GetLastInputInfo）；非 Windows 或调用失败返回 0。"""
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            tick = ctypes.windll.kernel32.GetTickCount()
            return max(0.0, (tick - lii.dwTime) / 1000.0)
    except Exception:
        pass
    return 0.0


def foreground_fullscreen() -> bool:
    """是否有「真全屏」窗口占据所在显示器（看视频 / 演示 / 全屏游戏）。

    只凭空闲时长判断熄屏会把整场电影误判成熄屏（鼠标键盘不动，但屏幕亮着），
    白白少算显示器那一档功耗。这里用窗口特征区分：
      · 真全屏：无标题栏，或置顶 —— 视频播放器 F11、PPT 放映、全屏游戏
      · 最大化：仍带标题栏 —— 人可能只是开着窗口走开了，不算
    非 Windows 或探测失败一律返回 False（宁可按空闲判断，不改变既有行为）。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        u = ctypes.windll.user32
        u.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        u.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        u.GetMonitorInfoW.argtypes = (ctypes.c_ulong, ctypes.POINTER(_MONITORINFO))

        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False

        GWL_STYLE, GWL_EXSTYLE = -16, -20
        WS_CAPTION, WS_EX_TOPMOST = 0x00C00000, 0x00000008
        style = u.GetWindowLongPtrW(hwnd, GWL_STYLE)
        exstyle = u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        # 有标题栏且不置顶 → 普通窗口/最大化，不算全屏
        if (style & WS_CAPTION) and not (exstyle & WS_EX_TOPMOST):
            return False

        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return False

        # 按窗口所在的那块屏判断，多屏环境下才准
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        hmon = u.MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
        if hmon and u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            mw = mi.rcMonitor.right - mi.rcMonitor.left
            mh = mi.rcMonitor.bottom - mi.rcMonitor.top
        else:
            mw = u.GetSystemMetrics(0)               # SM_CXSCREEN
            mh = u.GetSystemMetrics(1)               # SM_CYSCREEN
        if mw <= 0 or mh <= 0:
            return False
        return (r.right - r.left) >= mw and (r.bottom - r.top) >= mh
    except Exception:
        return False


def display_auto_off(grace: float = 3.0) -> bool:
    """推断系统是否已自动熄屏：空闲时长超过系统熄屏超时（留 grace 秒余量）。

    例外：有真全屏窗口时（看视频/放映/全屏游戏）即便长时间无输入也视为屏幕亮着。
    """
    try:
        if foreground_fullscreen():
            return False
        return user_idle_sec() >= (display_off_timeout_sec() + grace)
    except Exception:
        return False


if __name__ == "__main__":
    h = detect_hardware()
    print("CPU:", h.cpu_name, h.cpu_cores, "C", h.cpu_threads, "T")
    print("GPU:", h.gpu_name, h.gpu_resolution, "NVIDIA=", h.gpu_is_nvidia)
    print("RAM:", human_bytes(h.ram_bytes))
    print("Disks:", h.disks)
    print("Monitors:", h.monitor_count, "Battery:", h.has_battery)
    import time
    for _ in range(3):
        print("sample:", sample_load())
        time.sleep(1)
