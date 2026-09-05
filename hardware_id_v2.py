# -*- coding: utf-8 -*-
"""硬件型号识别 v2 —— 针对 v18.29 审查报告 第 5 节的落地补丁。

解决三个实测问题（数据见报告）：

A. 静默错认 / 缺失 —— 表未覆盖的型号会「安静地」落到启发式，误差最大 69%
   RTX 3050 8GB  现: 220W(VRAM启发式)  真: 130W  → +69%
   Core Ultra 9 285K 现: 65W(兜底默认)  真: 125W  → -48%
   Core i9-14900KF   现: 105W(兜底)     真: 125W  → -16%
   RTX 4090 D        现: 450W(错认4090) 真: 425W  → +6%
   Intel Arc A770    现: 300W(VRAM)     真: 225W  → +33%

B. 后缀变体全军覆没 —— 词边界 (?<!\\w)..(?!\\w) 使 'i5-12400F' 匹配不到
   'i5-12400'（F 是 \\w，负向断言失败）。KF/F/S/T 后缀需显式处理。

C. 性能 —— _find_key 180 µs/次，其中 51% 耗在对「每个键」重复跑 _norm_key。
   表若从 48 键扩到 200+ 键会线性劣化。改为预编译索引后 ~2x。

设计原则：
  * 不改 power_model.py 原表，仅以「增量补丁表」形式提供，合并成本 = 一行 dict.update
  * _find_key_v2 保持与 _find_key 完全相同的签名与返回值，可原地替换
  * 新增 identify_gpu/identify_cpu 返回 (瓦数, 置信度, 命中方式)，
    让 UI 能提示「型号未识别，建议校准」——把静默误差变成可见信号
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import power_model as PM

# --------------------------------------------------------------------------- #
# 1) 增量型号表：只列 v18.29 缺失/错认的型号，键名风格与 power_model 保持一致
# --------------------------------------------------------------------------- #

GPU_TDP_EXTRA: Dict[str, int] = {
    # --- NVIDIA RTX 30 系补全（3050 是最大缺口：普及度极高却完全缺失）---
    "RTX 3050": 130, "RTX 3050 Ti": 130,
    "RTX 2060 Super": 175, "RTX 2070 Super": 215, "RTX 2080 Super": 250,
    "RTX 3080 12GB": 350, "RTX 3090 Ti": 450,
    # --- RTX 40 系变体（4090 D 为中国特供，TDP 低于标准版）---
    "RTX 4090 D": 425,
    "RTX 4070 Ti Super": 285, "RTX 4080 Super": 320,
    # --- RTX 50 系补全 ---
    "RTX 5060 Ti": 180, "RTX 5070 Ti": 300,
    # --- AMD RX 补全 ---
    "RX 5500 XT": 130, "RX 5600 XT": 160, "RX 5700 XT": 225,
    "RX 6600 XT": 160, "RX 6700": 175, "RX 6750 XT": 250, "RX 6950 XT": 335,
    # --- Intel Arc 独显（v18.29 完全缺失，整代都认不出）---
    "Arc A770": 225, "Arc A750": 225, "Arc A580": 185,
    "Arc B580": 190, "Arc B570": 150,
    # --- GTX 16/10 系遗留 ---
    "GTX 1650 Super": 100, "GTX 1660 Ti": 120, "GTX 1050": 75, "GT 1030": 30,
}

CPU_TDP_EXTRA: Dict[str, int] = {
    # --- Intel 第 12/13/14 代 F / KF 后缀（无核显版，同样普及）---
    "i5-12400F": 65, "i5-13400F": 65, "i5-14400F": 65,
    "i7-13700F": 65, "i9-14900F": 65,
    "i5-12600KF": 125, "i7-12700KF": 125, "i9-12900KF": 125,
    "i5-13600KF": 125, "i7-13700KF": 125, "i9-13900KF": 125,
    "i9-14900KF": 125,
    "i5-12490F": 65,                      # 中国特供
    # --- Intel Core Ultra 系列（新命名，v18.29 完全不匹配 -> 兜底 65W）---
    "Ultra 5 245K": 125, "Ultra 7 265K": 125, "Ultra 9 285K": 125,
    "Ultra 5 245KF": 125, "Ultra 7 265KF": 125, "Ultra 9 285KF": 125,
    "Ultra 5 245": 65, "Ultra 7 265": 65, "Ultra 9 285": 65,
    # --- AMD AM4/AM5 补全 ---
    "5700X3D": 105, "7600X3D": 65, "7900X3D": 120, "7950X3D": 120,
    "7500F": 65, "8600G": 65, "8700G": 65, "5700": 65, "5900": 65,
    "9600": 65, "9700": 65,
}

# 合并后的完整表（供 v2 使用；落地时可直接 power_model.GPU_TDP.update(GPU_TDP_EXTRA)）
GPU_TDP_V2: Dict[str, int] = {**PM.GPU_TDP, **GPU_TDP_EXTRA}
CPU_TDP_V2: Dict[str, int] = {**PM.CPU_TDP, **CPU_TDP_EXTRA}

# CPU 型号尾缀：按「剥离优先级」排列，先剥长后缀
_CPU_SUFFIXES = ("KF", "F", "K", "S", "T", "HX", "H", "U", "G")


# --------------------------------------------------------------------------- #
# 2) 预编译索引：把 _norm_key(键) + 正则编译 的一次性成本挪到表构建时
# --------------------------------------------------------------------------- #
class _Index:
    """对一张型号表预计算 (归一化键, 已编译正则, 键长)，按长度降序以便最长匹配早停。"""

    __slots__ = ("items",)

    def __init__(self, table: Dict[str, int]):
        rows = []
        for k, v in table.items():
            kl = PM._norm_key(k)
            if not kl:
                continue
            pat = re.compile(r"(?<![\w])" + re.escape(kl) + r"(?![\w])")
            rows.append((len(kl), pat, v))
        rows.sort(key=lambda r: -r[0])          # 长键优先 → 首个命中即最长
        self.items = rows


@lru_cache(maxsize=8)
def _index_for(table_id: int, table_len: int) -> _Index:  # pragma: no cover - 缓存壳
    raise RuntimeError("unreachable")


_INDEX_CACHE: Dict[int, _Index] = {}


def _get_index(table: Dict[str, int]) -> _Index:
    """按 (id, 长度, 键集合指纹) 缓存索引；表被 update 后指纹变化会自动重建。"""
    key = (id(table), len(table), hash(frozenset(table.keys())))
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        idx = _Index(table)
        # 简单容量控制
        if len(_INDEX_CACHE) > 16:
            _INDEX_CACHE.clear()
        _INDEX_CACHE[key] = idx
    return idx


# --------------------------------------------------------------------------- #
# 3) 主匹配函数
# --------------------------------------------------------------------------- #
def _strip_cpu_suffix(name: str) -> List[str]:
    """生成剥离尾缀后的候选名：'i9-14900KF' -> ['i9-14900KF','i9-14900','i9-14900K']。

    只剥「紧跟数字」的后缀，且要求剥离后仍以数字结尾，避免误伤
    'Ryzen 5 7500F' 之外的普通词。
    """
    s = PM._norm_key(name)
    out = [s]
    base = s.rstrip()
    for suf in _CPU_SUFFIXES:
        low = base.lower()
        if low.endswith(suf.lower()) and len(base) > len(suf):
            cand = base[: -len(suf)].rstrip()
            if cand and cand[-1].isdigit():
                out.append(cand)
    return out


def find_key_v2(name: str, table: Dict[str, int],
                allow_suffix_strip: bool = False) -> Optional[int]:
    """最长匹配 + 预编译索引。**签名与 power_model._find_key 完全一致**，可原地替换。

    allow_suffix_strip=True 时（仅建议 CPU 表开启），在未命中情况下再按
    KF/F/K/… 尾缀剥离重试一輪，解决 'i5-12400F' 匹配不到 'i5-12400' 的问题。
    """
    if not name:
        return None
    idx = _get_index(table)
    cands = _strip_cpu_suffix(name) if allow_suffix_strip else [PM._norm_key(name)]
    for cand in cands:
        for _len, pat, val in idx.items:                 # 已按长度降序
            if pat.search(cand):
                return val                               # 首个命中即最长
    return None


# --------------------------------------------------------------------------- #
# 4) 带置信度的识别入口（推荐 UI 使用）
# --------------------------------------------------------------------------- #
def identify_gpu(name: str, vram_gb: float = 0.0) -> Tuple[int, str, str]:
    """返回 (瓦数, 置信度, 命中方式)。置信度: high / medium / low。

    - high   : 型号表精确命中
    - medium : 尾缀剥离后命中
    - low    : 未识别，走显存启发式（误差可达 ±70%，UI 应提示校准）
    """
    v = find_key_v2(name, GPU_TDP_V2)
    if v is not None:
        return int(v), "high", "table"
    v = find_key_v2(name, GPU_TDP_V2, allow_suffix_strip=True)
    if v is not None:
        return int(v), "medium", "suffix-strip"
    return int(_gpu_fallback(vram_gb)), "low", "vram-heuristic"


def identify_cpu(name: str) -> Tuple[int, str, str]:
    """返回 (瓦数, 置信度, 命中方式)。"""
    v = find_key_v2(name, CPU_TDP_V2)
    if v is not None:
        return int(v), "high", "table"
    v = find_key_v2(name, CPU_TDP_V2, allow_suffix_strip=True)
    if v is not None:
        return int(v), "medium", "suffix-strip"
    # 兜底：v18.29 在此处无条件给 65W，新命名（Core Ultra）会低估近一半。
    # v2 保留同样兜底值，但把置信度标为 low，让上层能提示用户。
    return int(_cpu_fallback(name)), "low", "name-heuristic"


def _gpu_fallback(vram_gb: float) -> int:
    """显存启发式。v18.29 原实现；此处保留数值不变以保证行为可比对，
    但调用方会收到 confidence='low'。**真正建议是补表，而非调这个 heuristic**。"""
    if vram_gb >= 20:
        return 400
    if vram_gb >= 12:
        return 300
    if vram_gb >= 8:
        return 220
    if vram_gb >= 4:
        return 150
    return 75


def _cpu_fallback(name: str) -> int:
    """型号名启发式（与 power_model.build_model 现有逻辑一致）。"""
    if re.search(r"Ryzen\s*[79]", name, re.I) or re.search(r"i[79]-1[34]", name):
        return 105
    if re.search(r"Ryzen\s*5", name, re.I) or re.search(r"i[35]", name):
        return 65
    return 65


def needs_calibration_hint(conf: str) -> bool:
    """供 UI 调用：低置信度时应提示用户做实测校准。"""
    return conf == "low"


# --------------------------------------------------------------------------- #
# 5) 自测：v1 vs v2 对照 + 性能
# --------------------------------------------------------------------------- #
def _self_test() -> None:
    cases_gpu = [
        # (型号, 显存GB, 真实TDP, 说明)
        ("NVIDIA GeForce RTX 3050 8GB", 8, 130, "最大缺口：普及入门卡"),
        ("NVIDIA GeForce RTX 3060 Ti", 8, 200, "回归：不得退化为 3060"),
        ("NVIDIA GeForce RTX 3060", 12, 170, "回归"),
        ("NVIDIA GeForce GTX 1660 Super", 6, 125, "回归"),
        ("Intel Arc A770 Graphics", 16, 225, "整代缺失"),
        ("AMD Radeon RX 6750 XT", 12, 250, "缺失"),
        ("NVIDIA GeForce RTX 4090 D", 24, 425, "错认成 4090"),
        ("AMD Radeon RX 6800 XT", 16, 300, "回归"),
        ("AMD Radeon Graphics", 2, 25, "回归：核显不得误套独显"),
        ("Intel UHD Graphics 630", 0, 15, "回归：填充词归一化"),
    ]
    cases_cpu = [
        ("Intel Core Ultra 9 285K", 125, "新命名 -> 曾兜底 65W"),
        ("Intel Core i9-14900KF", 125, "KF 后缀 -> 曾兜底 105W"),
        ("Intel Core i5-12400F", 65, "F 后缀"),
        ("Intel Core i5-14600K", 125, "回归：不得退化为 14600"),
        ("AMD Ryzen 7 7800X3D", 120, "回归"),
        ("AMD Ryzen 7 5700X3D", 105, "缺失"),
    ]

    print("=" * 78)
    print("GPU 识别：v18.29 现状(v1)  vs  v2")
    print("=" * 78)
    print(f"{'型号':<34}{'真实':>6}{'v1':>7}{'v1误差':>9}{'v2':>7}{'v2误差':>9}  置信")
    v1_bad = v2_bad = 0
    for name, vram, real, note in cases_gpu:
        v1 = PM._find_key(name, PM.GPU_TDP)
        v1v = v1 if v1 is not None else _gpu_fallback(vram)
        v2v, conf, _m = identify_gpu(name, vram)
        e1 = (v1v - real) / real * 100
        e2 = (v2v - real) / real * 100
        v1_bad += abs(e1) > 10
        v2_bad += abs(e2) > 10
        print(f"{name:<34}{real:>6}{v1v:>7}{e1:>+8.0f}%{v2v:>7}{e2:>+8.0f}%  {conf}")
    print(f"\n  v1 误差>10% 的型号: {v1_bad}/{len(cases_gpu)}")
    print(f"  v2 误差>10% 的型号: {v2_bad}/{len(cases_gpu)}")

    print()
    print("=" * 78)
    print("CPU 识别：v18.29 现状(v1)  vs  v2")
    print("=" * 78)
    print(f"{'型号':<34}{'真实':>6}{'v1':>7}{'v1误差':>9}{'v2':>7}{'v2误差':>9}  置信")
    c1_bad = c2_bad = 0
    for name, real, note in cases_cpu:
        v1 = PM._find_key(name, PM.CPU_TDP)
        v1v = v1 if v1 is not None else _cpu_fallback(name)
        v2v, conf, _m = identify_cpu(name)
        e1 = (v1v - real) / real * 100
        e2 = (v2v - real) / real * 100
        c1_bad += abs(e1) > 10
        c2_bad += abs(e2) > 10
        print(f"{name:<34}{real:>6}{v1v:>7}{e1:>+8.0f}%{v2v:>7}{e2:>+8.0f}%  {conf}")
    print(f"\n  v1 误差>10% 的型号: {c1_bad}/{len(cases_cpu)}")
    print(f"  v2 误差>10% 的型号: {c2_bad}/{len(cases_cpu)}")

    # 性能
    import time
    n = 20000
    t = time.perf_counter()
    for _ in range(n):
        PM._find_key("NVIDIA GeForce RTX 4070 Ti SUPER", PM.GPU_TDP)
    d1 = time.perf_counter() - t
    t = time.perf_counter()
    for _ in range(n):
        find_key_v2("NVIDIA GeForce RTX 4070 Ti SUPER", GPU_TDP_V2)
    d2 = time.perf_counter() - t
    print()
    print("=" * 78)
    print(f"性能（{n} 次调用，v2 表 {len(GPU_TDP_V2)} 键 vs v1 表 {len(PM.GPU_TDP)} 键）")
    print(f"  v1: {d1/n*1e6:7.1f} µs/次")
    print(f"  v2: {d2/n*1e6:7.1f} µs/次   -> {d1/d2:.1f}x（表更大反而更快）")

    # 断言
    assert identify_gpu("NVIDIA GeForce RTX 3050 8GB", 8)[0] == 130
    assert identify_gpu("NVIDIA GeForce RTX 3060 Ti", 8)[0] == 200
    assert identify_gpu("Intel Arc A770 Graphics", 16)[0] == 225
    assert identify_gpu("AMD Radeon Graphics", 2)[0] == 25
    assert identify_cpu("Intel Core Ultra 9 285K")[0] == 125
    assert identify_cpu("Intel Core i9-14900KF")[0] == 125
    assert identify_cpu("Intel Core i5-14600K")[0] == 125
    assert identify_gpu("NVIDIA GeForce RTX 3060", 12)[1] == "high"
    assert identify_cpu("Some Unknown CPU-X1")[1] == "low"
    print()
    print("ALL OK: hardware_id_v2 自测通过")


if __name__ == "__main__":
    _self_test()
