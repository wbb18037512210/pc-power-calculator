# -*- coding: utf-8 -*-
"""PC 功耗模型：部件 TDP 查表 + 实时负载估算 + 插座功耗（含电源损耗）折算。

模型分两类：
  * 动态部件（CPU/GPU）：随负载变化，实时采样插值。GPU 若支持 nvidia-smi 则用真实功耗。
  * 静态部件（主板/内存/硬盘/风扇/显示器/外设）：近似恒定，按配置给出经验值。

插座功耗 = 系统功耗 / 电源效率（默认 0.85）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ---------- 常见桌面 CPU TDP 查表（W，标称 TDP；满载按 boost 系数放大） ----------
CPU_TDP = {
    # AMD Ryzen 桌面
    "5600X": 65, "5600": 65, "5600G": 65, "5700X": 65, "5700G": 65, "5800X": 105,
    "5800X3D": 105, "5900X": 105, "5950X": 105, "5500": 65,
    "7600X": 105, "7600": 65, "7700X": 105, "7700": 65, "7800X3D": 120,
    "7900X": 170, "7950X": 170, "9600X": 65, "9700X": 65, "9800X3D": 120,
    "9900X": 120, "9950X": 170,
    # Intel 桌面（含 K）
    "i3-12100": 60, "i5-12400": 65, "i5-12600K": 125, "i7-12700K": 125, "i9-12900K": 125,
    "i5-13400": 65, "i5-13600K": 125, "i7-13700K": 125, "i9-13900K": 125,
    "i5-14400": 65, "i5-14600K": 125, "i7-14700K": 125, "i9-14900K": 125,
    "i5-14600KF": 125, "i7-14700KF": 125,
}

# ---------- 常见桌面 GPU TDP 查表（W） ----------
GPU_TDP = {
    "GTX 1050 Ti": 75, "GTX 1060": 120, "GTX 1070": 150, "GTX 1080": 180,
    "GTX 1080 Ti": 250, "GTX 1650": 75, "GTX 1660": 120, "GTX 1660 Super": 125,
    "RTX 2060": 160, "RTX 2070": 175, "RTX 2080": 215, "RTX 2080 Ti": 250,
    "RTX 3060": 170, "RTX 3060 Ti": 200, "RTX 3070": 220, "RTX 3080": 320,
    "RTX 3090": 350, "RTX 4060": 115, "RTX 4060 Ti": 165, "RTX 4070": 200,
    "RTX 4070 Super": 220, "RTX 4070 Ti": 285, "RTX 4080": 320, "RTX 4080 Super": 320,
    "RTX 4090": 450, "RTX 5060": 145, "RTX 5070": 250, "RTX 5080": 360, "RTX 5090": 575,
    "RX 580": 185, "RX 5700": 180, "RX 6600": 132, "RX 6650 XT": 176,
    "RX 6700 XT": 230, "RX 6800": 250, "RX 6800 XT": 300, "RX 6900 XT": 300,
    "RX 7600": 165, "RX 7700 XT": 245, "RX 7800 XT": 263, "RX 7900 XT": 315,
    "RX 7900 XTX": 355,
    # 核显（粗估满载）
    "UHD 630": 15, "UHD 750": 15, "UHD 770": 15, "Vega 8": 25, "Vega 11": 35,
    "Radeon Graphics": 25,
}

# 静态部件经验功耗（W）
STATIC = {
    "motherboard_idle": 35.0,      # 主板+芯片组+VRM 空载
    "motherboard_load_add": 18.0,  # 满载额外
    "ram_per_8gb": 3.0,
    "ssd": 2.5,
    "hdd": 7.0,
    "fan": 8.0,                    # 机箱风扇+CPU风扇
    "monitor_1440p": 30.0,         # 每台显示器（按 1440p 估算）
    "monitor_1080p": 22.0,
    "monitor_4k": 45.0,
    "peripheral": 5.0,             # 键鼠/USB
}
PSU_EFFICIENCY = 0.85  # 电源转换效率（铜牌≈0.85，金牌≈0.90）

# v18.11 动态效率曲线：80 PLUS 金牌典型曲线（负载率 -> 转换效率）。
# 电源效率并非恒定值——轻载（<20%）和满载都偏低，50% 附近最高，
# 固定 0.85 会在低负载时高估、高负载时低估实际插墙功耗。
PSU_EFF_CURVE = [(0.05, 0.70), (0.10, 0.82), (0.20, 0.87), (0.30, 0.895),
                 (0.50, 0.90), (0.75, 0.88), (1.00, 0.85)]


def psu_eff_at(load_w: float, rating_w: float):
    """按当前负载率查电源效率曲线（线性插值）。

    rating_w <= 0 表示用户未设置电源额定功率，返回 None（沿用固定效率，
    保证未设置时行为与旧版完全一致）。
    """
    if rating_w is None or rating_w <= 0:
        return None
    pts = PSU_EFF_CURVE
    frac = max(0.02, min(1.0, float(load_w) / float(rating_w)))
    if frac <= pts[0][0]:
        return pts[0][1]
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        if frac <= x1:
            t = (frac - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return pts[-1][1]


@dataclass
class PowerModel:
    cpu_tdp: float = 65.0
    cpu_idle: float = 20.0
    cpu_max: float = 95.0
    gpu_tdp: float = 180.0
    gpu_idle: float = 15.0
    gpu_max: float = 180.0
    gpu_is_nvidia: bool = False
    # v18.29+ 识别置信度：high=表精确命中 / medium=后缀剥离命中 / low=走启发式（可能偏差大）
    cpu_conf: str = "high"
    gpu_conf: str = "high"
    static_idle: float = 0.0
    static_load_add: float = 0.0
    psu_efficiency: float = PSU_EFFICIENCY
    psu_rating_w: float = 0.0      # 电源额定功率 W；0=未设置，沿用固定效率
    components: dict = field(default_factory=dict)
    # 校准：优先用「实测待机/满载」两点线性映射；未填则用单系数倍率 k
    calib_k: float = 1.0
    calib_idle: float = 0.0
    calib_peak: float = 0.0
    # v18.11 显示器功耗：独立市电供电，既不经过 PC 电源转换，
    # 也不参与主机实测校准映射，否则关屏扣除量会被校准线性放大
    monitor_w: float = 0.0

    @property
    def peak_sys(self) -> float:
        return self.cpu_max + self.gpu_max + self.static_idle + self.static_load_add

    @property
    def idle_sys(self) -> float:
        return self.cpu_idle + self.gpu_idle + self.static_idle

    def calibrated(self, host_w: float) -> float:
        """把模型「主机」功耗映射到校准后的主机功耗。

        v18.11：只接受不含显示器的主机功耗，映射区间同样剔除显示器，
        保证开关显示器不会扭曲校准结果。
        """
        idle = self.idle_sys - self.monitor_w
        peak = self.peak_sys - self.monitor_w
        if self.calib_idle > 0 and self.calib_peak > 0 and peak > idle:
            frac = (host_w - idle) / (peak - idle)
            frac = max(0.0, min(1.0, frac))
            return self.calib_idle + frac * (self.calib_peak - self.calib_idle)
        return host_w * self.calib_k


def _norm_key(s: str) -> str:
    """归一化型号字符串，便于稳定匹配。
    * 去 (R)/(TM)/®/™ 商标符；
    * 仅「Intel 系」（UHD/HD/Iris）后方才剥离 'graphics' 填充词，
      使 'Intel UHD Graphics 630' -> 'intel uhd 630' 能命中 'UHD 630'。
      —— 注意不能无差别剥离：键 'Radeon Graphics' 经 \bgraphics\b 会变成
      'radeon'，导致它误匹配所有 Radeon 独显（如 RX 5800 XT 被错认成 25W
      核显）。故只在 Intel 家族词后剥离。
    * 折叠空白。
    """
    s = (s or "").lower()
    s = re.sub(r"\(r\)|\(tm\)|®|™", "", s)
    s = re.sub(r"\b(?:intel\s+)?(?:u|uhd|hd|iris|iris\s+xe)\s+graphics\b",
               lambda m: m.group(0).replace(" graphics", ""), s)
    return re.sub(r"\s+", " ", s).strip()


def _find_key(name: str, table: dict) -> Optional[int]:
    """最长匹配优先：型号字符串可能同时包含 'RTX 3060' 与 'RTX 3060 Ti'，
    必须取最长（最具体）的 key，否则会先命中短前缀，把 Ti/SUPER 错认成低规格型号
    （RTX 3060 Ti -> 200 误为 170；GTX 1660 Super -> 125 误为 120）。

    词边界正则 (?<!\\w)...(?!\\w) 防止 'RX 580' 误中 'RX 5800' 之类。
    """
    n = _norm_key(name)
    best_v, best_len = None, -1
    for k, v in table.items():
        kl = _norm_key(k)
        if re.search(r"(?<![\w])" + re.escape(kl) + r"(?![\w])", n):
            if len(kl) > best_len:
                best_v, best_len = v, len(kl)
    return best_v


def build_model(hw, psu_efficiency: float = PSU_EFFICIENCY) -> PowerModel:
    m = PowerModel(psu_efficiency=psu_efficiency, gpu_is_nvidia=hw.gpu_is_nvidia)

    # CPU（v18.29+：改用 identify_cpu，带置信度；未识别时回落原启发式，语义不变）
    tdp, cpu_conf, _m = identify_cpu(hw.cpu_name)
    m.cpu_conf = cpu_conf
    m.cpu_tdp = float(tdp)
    m.cpu_idle = round(tdp * 0.30, 1)        # 空载约 30% TDP
    m.cpu_max = round(tdp * 1.40, 1)         # 满载含 boost 约 1.4x

    # GPU（v18.29+：改用 identify_gpu，带置信度；未识别时回落原显存启发式，语义不变）
    gtdp, gpu_conf, _m2 = identify_gpu(hw.gpu_name, hw.gpu_vram_bytes / 1e9)
    m.gpu_conf = gpu_conf
    m.gpu_tdp = float(gtdp)
    m.gpu_idle = round(max(8.0, gtdp * 0.08), 1)
    m.gpu_max = float(gtdp)

    # 静态部件
    comps = {}
    comps["主板/芯片组"] = STATIC["motherboard_idle"]
    ram_gb = hw.ram_bytes / 1e9
    comps["内存"] = round((ram_gb / 8.0) * STATIC["ram_per_8gb"], 1)
    ssd_n = sum(1 for t, _ in hw.disks if str(t).upper() == "SSD")
    hdd_n = sum(1 for t, _ in hw.disks if str(t).upper() == "HDD")
    comps["SSD"] = round(ssd_n * STATIC["ssd"], 1)
    comps["HDD"] = round(hdd_n * STATIC["hdd"], 1)
    comps["风扇"] = STATIC["fan"]
    # 显示器分辨率估算
    mon_w = STATIC["monitor_1440p"]
    if "3840" in hw.gpu_resolution or "2160" in hw.gpu_resolution:
        mon_w = STATIC["monitor_4k"]
    elif "1920" in hw.gpu_resolution:
        mon_w = STATIC["monitor_1080p"]
    comps["显示器"] = round(hw.monitor_count * mon_w, 1)
    comps["外设"] = STATIC["peripheral"]

    m.static_idle = round(sum(comps.values()), 1)
    m.static_load_add = STATIC["motherboard_load_add"]
    m.components = comps
    m.monitor_w = float(comps.get("显示器", 0.0) or 0.0)
    return m


def estimate(model: PowerModel, cpu_load: float, gpu_power: Optional[float],
             gpu_valid: bool, display_on: bool = True) -> dict:
    """根据实时采样估算瞬时功耗。

    display_on=False 时按显示器已关闭处理，从静态项中扣除显示器功耗
    （显示器由市电直供，不经过电源转换，故扣在 PSU 效率折算之前）。

    返回：{sys_watts, wall_watts, cpu_w, gpu_w, static_w, breakdown, display_w}
    """
    cpu_frac = max(0.0, min(1.0, cpu_load / 100.0))
    cpu_w = model.cpu_idle + cpu_frac * (model.cpu_max - model.cpu_idle)

    if gpu_valid and gpu_power is not None:
        gpu_w = float(gpu_power)               # N 卡真实功耗
    else:
        # 非 N 卡：用 CPU 负载近似 GPU 负载
        gpu_frac = cpu_frac
        gpu_w = model.gpu_idle + gpu_frac * (model.gpu_max - model.gpu_idle)

    # v18.11 显示器：独立市电供电，关屏归零，
    # 且不进入主机的实测校准映射与电源效率折算链路
    disp_w = model.monitor_w if display_on else 0.0
    static_w = (model.static_idle - model.monitor_w) + cpu_frac * model.static_load_add
    host_w = cpu_w + gpu_w + static_w          # 主机 DC 功耗（不含显示器）
    calib_host = model.calibrated(host_w)      # 校准只作用于主机
    # v18.11 动态效率：设了电源额定功率就按负载率查曲线，否则沿用固定值
    _eff = psu_eff_at(calib_host, getattr(model, "psu_rating_w", 0.0))
    if _eff is None:
        _eff = model.psu_efficiency
    wall_w = calib_host / _eff + disp_w
    sys_w = host_w + disp_w                    # 展示用整机功耗（含显示器）

    breakdown = dict(model.components)
    breakdown["显示器"] = round(disp_w, 1)
    breakdown["CPU"] = round(cpu_w, 1)
    breakdown["GPU"] = round(gpu_w, 1)

    return {
        "sys_watts": round(sys_w, 1),
        "calib_sys_watts": round(calib_host + disp_w, 1),
        "wall_watts": round(wall_w, 1),
        "cpu_w": round(cpu_w, 1),
        "gpu_w": round(gpu_w, 1),
        "static_w": round(static_w, 1),
        "display_w": round(disp_w, 1),
        "psu_eff": round(_eff, 4),        # 实际采用的转换效率（供 UI 展示）
        "breakdown": breakdown,
    }


# --------------------------------------------------------------------------- #
# v18.29+ 型号识别增强（审查报告 §5.1）：补表 + 后缀剥离 + 预编译索引 + 置信度
# 放在文件末尾：此时本模块已完整定义，可安全 import hardware_id_v2（其模块级
# 会读取 PM.GPU_TDP）。整段包在 try 里：v2 模块缺失时退回原 _find_key 实现，
# 行为不退化。
# --------------------------------------------------------------------------- #
try:
    from hardware_id_v2 import (
        GPU_TDP_EXTRA, CPU_TDP_EXTRA, find_key_v2,
        identify_gpu, identify_cpu, needs_calibration_hint,
    )
    GPU_TDP.update(GPU_TDP_EXTRA)                 # 增量补表（+27 GPU / +31 CPU 型号）
    CPU_TDP.update(CPU_TDP_EXTRA)
    _find_key = lambda n, t: find_key_v2(n, t, allow_suffix_strip=True)  # 原地替换，签名一致
except Exception:  # pragma: no cover - v2 缺失属异常环境，退回原实现
    pass


if __name__ == "__main__":
    import hardware as H
    h = H.detect_hardware()
    m = build_model(h)
    print("CPU TDP", m.cpu_tdp, "idle", m.cpu_idle, "max", m.cpu_max)
    print("GPU TDP", m.gpu_tdp, "idle", m.gpu_idle, "max", m.gpu_max)
    print("static_idle", m.static_idle, "peak_sys", m.peak_sys, "idle_sys", m.idle_sys)
    print("components", m.components)
    print("estimate@10%:", estimate(m, 10, 50, True))
    print("estimate@100%:", estimate(m, 100, 180, True))
