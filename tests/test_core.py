# -*- coding: utf-8 -*-
"""纯逻辑测试（不依赖 Qt）：核心识别 + 计量引擎。

运行：
    python -m pytest tests/test_core.py -q
或（无 pytest 时）：
    python tests/test_core.py
"""
import os
import sys

# 让测试能 import 到项目根目录的 power_model / power_core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import power_model as PM
import power_core as PC


# ----------------------------- _find_key 准确性（W3） -----------------------------
def test_find_key_ti_not_misdetected_as_base():
    assert PM._find_key("NVIDIA GeForce RTX 3060 Ti", PM.GPU_TDP) == 200
    assert PM._find_key("NVIDIA GeForce RTX 3060", PM.GPU_TDP) == 170


def test_find_key_super_not_misdetected_as_base():
    assert PM._find_key("NVIDIA GeForce GTX 1660 Super", PM.GPU_TDP) == 125
    assert PM._find_key("NVIDIA GeForce GTX 1660", PM.GPU_TDP) == 120


def test_find_key_word_boundary_rx6800_xt_not_base():
    # 词边界 + 最长匹配：'rx 6800' 不应误中 'rx 6800 xt'（两者在表中均存在）
    assert PM._find_key("AMD Radeon RX 6800", PM.GPU_TDP) == 250
    assert PM._find_key("AMD Radeon RX 6800 XT", PM.GPU_TDP) == 300  # 取最长，而非被 250 截短
    # 同理 'RX 6700 XT' 命中 230，不被更短前缀误伤
    assert PM._find_key("AMD Radeon RX 6700 XT", PM.GPU_TDP) == 230


def test_find_key_radeon_graphics_only_matches_integrated():
    # 回归：'Radeon Graphics'（核显，25W）绝不能因剥离 'graphics' 后变成 'radeon'，
    # 从而误匹配所有 Radeon 独显（如 RX 5800 XT）
    assert PM._find_key("AMD Radeon Graphics", PM.GPU_TDP) == 25
    # 独显 'RX 5800 XT' 不应被核显 25W 误匹配；且表中无 RX 5800 键、
    # 'RX 580' 又因词边界不会误中 'RX 5800'，因此应为无命中
    assert PM._find_key("AMD Radeon RX 5800 XT", PM.GPU_TDP) != 25
    assert PM._find_key("AMD Radeon RX 5800 XT", PM.GPU_TDP) is None


def test_find_key_normalizes_graphics_filler():
    # 'Intel UHD Graphics 630' 被 graphics 隔断，归一化后应命中 'UHD 630'
    assert PM._find_key("Intel UHD Graphics 630", PM.GPU_TDP) == 15


def test_find_key_strips_trademark_symbols():
    assert PM._find_key("NVIDIA GeForce RTX™ 3060 Ti", PM.GPU_TDP) == 200


def test_find_key_cpu_longest_match():
    # 短前缀不应抢先：i5-14600K 应命中 14600K(125) 而非 14600(65)
    assert PM._find_key("Intel Core i5-14600K", PM.CPU_TDP) == 125


# ----------------------------- PowerEngine 计量（W2/绞杀第一步） -----------------------------
def _mini_model():
    return PM.PowerModel(cpu_tdp=65, gpu_tdp=200, gpu_is_nvidia=True,
                         static_idle=80, static_load_add=18, monitor_w=30)


def test_engine_tick_accumulates_energy():
    eng = PC.PowerEngine.from_model(_mini_model(), {"price_mode": "峰谷"})
    t0 = 1_000_000.0
    last = None
    for i in range(5):
        now = t0 + i * 2.0
        eng.tick(cpu_load=40 + i * 5, gpu_power=100 + i * 8, gpu_valid=True,
                 disp_on=True, now_ts=now, last_ts=last)
        last = now
    assert eng.energy_wh > 0, "tick 应累计电量"
    seg = eng.period_energy_wh
    assert abs((seg["谷"] + seg["平"] + seg["峰"]) - eng.energy_wh) < 1e-6, seg


def test_engine_period_segmentation_is_exhaustive():
    eng = PC.PowerEngine.from_model(_mini_model())
    eng.tou_valley = (0, 0)   # 无谷段
    eng.tou_peak = []         # 无峰段 → 全部平段
    eng.price_mode = "峰谷"
    flat_only = eng.current_cost()
    eng.price_mode = "单一"; eng.rate = eng.rate_flat
    single = eng.current_cost()
    assert abs(flat_only - single) < 1e-6, (flat_only, single)


def test_engine_tiered_cost_monotonic():
    eng = PC.PowerEngine.from_model(_mini_model())
    c1 = eng._tiered_cost(1000.0)
    c2 = eng._tiered_cost(3000.0)
    c3 = eng._tiered_cost(6000.0)
    assert 0 < c1 < c2 < c3, (c1, c2, c3)


def test_engine_simulate_returns_positive_savings():
    eng = PC.PowerEngine.from_model(_mini_model(), {"price_mode": "峰谷"})
    t0 = 2_000_000.0
    last = None
    for i in range(5):
        now = t0 + i * 2.0
        eng.tick(cpu_load=40, gpu_power=120, gpu_valid=True,
                 disp_on=True, now_ts=now, last_ts=last)
        last = now
    r = eng.simulate(8.0, 0.95)
    assert r["sa"] > 0 and r["sb"] > 0, r


def test_engine_settings_roundtrip():
    eng = PC.PowerEngine.from_model(_mini_model(), {"price_mode": "阶梯",
                                                    "tier_l1": 2160.0, "tier_r1": 0.56})
    d = eng.to_settings_dict()
    assert d["price_mode"] == "阶梯"
    assert d["tou_valley"] == list(eng.tou_valley)
    eng2 = PC.PowerEngine.from_settings_dict(_mini_model(), d)
    assert eng2.price_mode == "阶梯"
    assert eng2.tier_l1 == 2160.0


if __name__ == "__main__":
    # 无 pytest 时，手动跑一遍
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    if failed:
        print(f"\n{ failed } 个用例失败")
        sys.exit(1)
    print(f"\nALL OK: { len(tests) } 个纯逻辑用例通过")
