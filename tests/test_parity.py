# -*- coding: utf-8 -*-
"""双实现漂移守卫：MainWindow 与 PowerEngine 的节能模拟必须给出相同数字。

对应审查报告 §5.2 W1：v18.29 新增 power_core.PowerEngine 但 main.py 零引用，
且 simulate 的基准效率 / 冷启动兜底 / 返回值三处已与 MainWindow 漂移。
本测试把 MainWindow._simulate 的等价复刻（mainwindow_simulate）与引擎对照，
任何一方改坏口径都会被立刻抓出。

运行：
    python tests/test_parity.py
    pytest tests/test_parity.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

import power_core as PC          # noqa: E402
import power_model as PM         # noqa: E402


def mainwindow_simulate(energy_wh, running_elapsed_ms, cur_wall, cur_psu_eff, psu_eff,
                        current_cost, rate, hours_off, target_eff) -> dict:
    """MainWindow._simulate 的等价复刻（逐行抄自 main.py，未做改动）。"""
    elapsed_h = max(running_elapsed_ms / 3_600_000.0, 1e-9)
    avg_w = (energy_wh / elapsed_h) if (energy_wh > 0 and elapsed_h > 1e-6) else (cur_wall or 0.0)
    kwh_total = energy_wh / 1000.0
    blended = (current_cost / kwh_total) if kwh_total > 0 else rate
    eff_old = cur_psu_eff or psu_eff                      # D1：动态效率优先
    sa_kwh = avg_w * hours_off / 1000.0 * 30.0
    sa = sa_kwh * blended
    wall_month = avg_w * 720.0 / 1000.0
    sys_month = wall_month * eff_old
    sb_kwh = sys_month * (1.0 / eff_old - 1.0 / target_eff) if target_eff > 0 else 0.0
    sb = sb_kwh * blended
    return {"avg_w": avg_w, "blended": blended, "wall_month": wall_month,
            "sa_kwh": sa_kwh, "sa": sa, "sb_kwh": sb_kwh, "sb": sb}


def _model(psu_rating_w: float) -> PM.PowerModel:
    return PM.PowerModel(cpu_tdp=65, gpu_tdp=200, gpu_is_nvidia=True,
                         static_idle=80, static_load_add=18, monitor_w=30,
                         psu_efficiency=0.85, psu_rating_w=psu_rating_w)


def test_simulate_matches_mainwindow():
    """热态（已累计 2h / 400Wh）：两者必须逐项一致。"""
    rate = 0.56
    eng = PC.PowerEngine.from_model(_model(650.0), {"price_mode": "单一", "rate": rate})
    eng.energy_wh = 400.0
    eng.running_elapsed_ms = 7_200_000.0
    cur_wall, cur_psu_eff, psu_eff = 239.4, 0.89, 0.85
    cost = 400.0 / 1000.0 * rate
    a = mainwindow_simulate(400.0, 7_200_000.0, cur_wall, cur_psu_eff, psu_eff,
                            cost, rate, 8.0, 0.95)
    b = eng.simulate(8.0, 0.95, cur_psu_eff=cur_psu_eff, cur_wall=cur_wall)
    for k in ("avg_w", "sa", "sb", "wall_month"):
        assert abs(a[k] - b[k]) < 1e-6, (k, a[k], b[k])


def test_simulate_cold_start():
    """冷启动（energy_wh==0）：avg_w 必须回落到当前 wall 功率，而非 0。"""
    rate = 0.56
    eng = PC.PowerEngine.from_model(_model(650.0), {"price_mode": "单一", "rate": rate})
    eng.running_elapsed_ms = 30_000.0
    cur_wall, cur_psu_eff = 239.4, 0.89
    a = mainwindow_simulate(0.0, 30_000.0, cur_wall, cur_psu_eff, 0.85, 0.0, rate, 8.0, 0.95)
    b = eng.simulate(8.0, 0.95, cur_psu_eff=cur_psu_eff, cur_wall=cur_wall)
    assert abs(a["avg_w"] - b["avg_w"]) < 1e-6, (a["avg_w"], b["avg_w"])
    assert abs((a["sa"] + a["sb"]) - (b["sa"] + b["sb"])) < 1e-6


if __name__ == "__main__":
    test_simulate_matches_mainwindow()
    test_simulate_cold_start()
    print("ALL OK: parity 测试通过（PowerEngine 与 MainWindow 口径一致）")
