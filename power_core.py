# -*- coding: utf-8 -*-
"""PowerEngine —— 零 Qt 依赖的核心计量引擎（对应评估报告 第5.2节）。

设计目标（绞杀者模式第一步）：
  把 MainWindow 里「与界面无关的全部计量逻辑」（瞬时估算、单一/峰谷/阶梯计费、
  峰谷分段累计、会话累计、idle 识别、节能模拟）搬到一个纯 Python 类里。

  * 本模块只依赖 power_model（同样零 Qt），可被 pytest 直接覆盖，无需拉起 Qt。
  * MainWindow 后续可持有 self.engine = PowerEngine(...)，把 on_sample 里的计费/
    累计逻辑逐步替换为 engine.tick(...) 委托；本文件先作为「被测核心」落地。
  * 计费配置（price_mode/rate/tier/tou…）可通过 from_settings_dict / to_settings_dict
    与 MainWindow 的设置字典对接，解决评估报告的 W4（设置序列化单点维护）。

用法：
  from power_core import PowerEngine
  eng = PowerEngine.from_model(model, settings_dict)
  est = eng.tick(cpu_load=40, gpu_power=100, gpu_valid=True, disp_on=True,
                 now_ts=time.time(), last_ts=None)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

import power_model as PM


@dataclass
class PowerEngine:
    """核心计量引擎：持有功耗模型 + 全部计费/累计状态，对外只暴露 tick()。"""

    model: PM.PowerModel
    price_mode: str = "单一"          # 单一 / 峰谷 / 阶梯
    rate: float = 0.56               # 单一电价 元/度
    rate_valley: float = 0.30        # 谷
    rate_flat: float = 0.56          # 平
    rate_peak: float = 0.85          # 峰
    tier_base: float = 0.0           # 阶梯：本月已用基数 kWh
    tier_l1: float = 2160.0
    tier_r1: float = 0.56
    tier_l2: float = 4800.0
    tier_r2: float = 0.61
    tier_r3: float = 0.86
    tou_valley: tuple = (23, 7)      # (起, 止) 小时，可跨午夜
    tou_peak: list = field(default_factory=list)  # [(起, 止), ...]

    # ---- 运行时累计状态 ----
    energy_wh: float = 0.0
    running_elapsed_ms: float = 0.0
    peak_wall: float = 0.0
    period_energy_wh: dict = field(default_factory=lambda: {"谷": 0.0, "平": 0.0, "峰": 0.0})
    idle_energy_wh: float = 0.0
    active_energy_wh: float = 0.0
    # v18.29+ 对齐 MainWindow 的运行时状态（消除双实现漂移，见审查报告 §5.2 D3）
    idle_streak_ms: float = 0.0
    disp_saved_wh: float = 0.0
    hourly: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    def tick(self, cpu_load: float, gpu_power: Optional[float], gpu_valid: bool,
             disp_on: bool, now_ts: float, last_ts: Optional[float]) -> dict:
        """喂一帧采样，返回本帧 estimate 并累加电量/电费分段。纯函数式，可单测。"""
        est = PM.estimate(self.model, cpu_load, gpu_power, gpu_valid, disp_on)
        if last_ts is not None:
            dt_ms = (now_ts - last_ts) * 1000.0
            if 0 < dt_ms < 60000:                    # 防跳变（跨零点/异常间隔）
                e = est["wall_watts"] * dt_ms / 3_600_000.0
                self.energy_wh += e
                self.period_energy_wh[self._period_of(now_ts)] += e
                self.running_elapsed_ms += dt_ms
                self.peak_wall = max(self.peak_wall, est["wall_watts"])
                is_idle = (cpu_load <= 5.0 and
                           (not gpu_valid or (gpu_power or 0.0) <= 15.0))
                if is_idle:
                    self.idle_energy_wh += e
                else:
                    self.active_energy_wh += e
        return est

    # ------------------------------------------------------------------ #
    def _period_of(self, epoch_s: float) -> str:
        h = datetime.fromtimestamp(epoch_s).hour
        vs, ve = self.tou_valley
        if vs <= ve:
            if vs <= h < ve:
                return "谷"
        else:
            if h >= vs or h < ve:           # 跨午夜（如 23→7）
                return "谷"
        for ps, pe in self.tou_peak:
            if ps <= h < pe:
                return "峰"
        return "平"

    def current_cost(self) -> float:
        if self.price_mode == "峰谷":
            return (self.period_energy_wh["谷"] / 1000.0 * self.rate_valley
                    + self.period_energy_wh["平"] / 1000.0 * self.rate_flat
                    + self.period_energy_wh["峰"] / 1000.0 * self.rate_peak)
        if self.price_mode == "阶梯":
            total = self.tier_base + self.energy_wh / 1000.0
            return self._tiered_cost(total) - self._tiered_cost(self.tier_base)
        return self.energy_wh / 1000.0 * self.rate

    def _tiered_cost(self, kwh: float) -> float:
        if kwh <= 0:
            return 0.0
        c = min(kwh, self.tier_l1) * self.tier_r1
        if kwh > self.tier_l1:
            c += (min(kwh, self.tier_l2) - self.tier_l1) * self.tier_r2
        if kwh > self.tier_l2:
            c += (kwh - self.tier_l2) * self.tier_r3
        return c

    def simulate(self, hours_off: float, target_eff: float,
                 cur_psu_eff: float = None, cur_wall: float = 0.0) -> dict:
        """节能情景模拟：与 MainWindow._simulate 逐项对齐（消除双实现漂移，§5.2 D1/D2/D3）。

        cur_psu_eff: 当前实时电源效率（来自 est['psu_eff']）；None 表示未启用动态曲线，
                     退回 model.psu_efficiency —— 与 MainWindow 的 `or` 语义一致。
        cur_wall   : 当前插座功率，用于冷启动（energy_wh==0）时的 avg_w 兜底。
        """
        elapsed_h = max(self.running_elapsed_ms / 3_600_000.0, 1e-9)
        avg_w = (self.energy_wh / elapsed_h) if (self.energy_wh > 0 and elapsed_h > 1e-6) \
            else (cur_wall or 0.0)                                   # D2
        kwh_total = self.energy_wh / 1000.0
        blended = (self.current_cost() / kwh_total) if kwh_total > 0 else self.rate
        eff_old = cur_psu_eff or self.model.psu_efficiency           # D1
        sa_kwh = avg_w * hours_off / 1000.0 * 30.0
        sa = sa_kwh * blended
        wall_month = avg_w * 720.0 / 1000.0                          # D3
        sys_month = wall_month * eff_old
        sb_kwh = sys_month * (1.0 / eff_old - 1.0 / target_eff) if target_eff > 0 else 0.0
        sb = sb_kwh * blended
        return {"avg_w": avg_w, "blended": blended, "wall_month": wall_month,
                "sa_kwh": sa_kwh, "sa": sa, "sb_kwh": sb_kwh, "sb": sb}

    # ------------------------------------------------------------------ #
    # 设置字典 <-> 引擎（对接 MainWindow 的 40 字段设置，解决 W4 单点维护）
    BILLING_KEYS = ("price_mode", "rate", "rate_valley", "rate_flat", "rate_peak",
                    "tier_base", "tier_l1", "tier_r1", "tier_l2", "tier_r2",
                    "tier_r3", "tou_valley", "tou_peak")

    def to_settings_dict(self) -> dict:
        """把计费配置导出为可被 MainWindow 写入会话的字典子集。"""
        d = {}
        for k in self.BILLING_KEYS:
            v = getattr(self, k)
            d[k] = list(v) if isinstance(v, (tuple, list)) else v
        return d

    @classmethod
    def from_settings_dict(cls, model: PM.PowerModel, d: Dict) -> "PowerEngine":
        """从 MainWindow 设置字典构造引擎（只读计费相关字段，缺省用默认值）。"""
        kw = {k: d[k] for k in cls.BILLING_KEYS if k in d}
        return cls(model=model, **kw)

    @classmethod
    def from_model(cls, model: PM.PowerModel, settings: Optional[Dict] = None) -> "PowerEngine":
        if settings:
            return cls.from_settings_dict(model, settings)
        return cls(model=model)


if __name__ == "__main__":
    # 直接运行可自测（python power_core.py），无需 Qt
    m = PM.PowerModel(cpu_tdp=65, gpu_tdp=200, gpu_is_nvidia=True,
                      static_idle=80, static_load_add=18, monitor_w=30)
    eng = PowerEngine.from_model(m, {"price_mode": "峰谷"})
    t0 = time.time()
    last = None
    for i in range(5):
        now = t0 + i * 2.0
        eng.tick(cpu_load=40 + i * 5, gpu_power=100 + i * 8, gpu_valid=True,
                 disp_on=True, now_ts=now, last_ts=last)
        last = now
    assert eng.energy_wh > 0, "tick 应累计电量"
    seg = eng.period_energy_wh
    assert abs((seg["谷"] + seg["平"] + seg["峰"]) - eng.energy_wh) < 1e-6, seg
    eng.tou_valley = (0, 0); eng.tou_peak = []
    eng.price_mode = "峰谷"
    flat_only = eng.current_cost()
    eng.price_mode = "单一"; eng.rate = eng.rate_flat
    single = eng.current_cost()
    assert abs(flat_only - single) < 1e-6, (flat_only, single)
    r = eng.simulate(8.0, 0.95)
    assert r["sa"] > 0 and r["sb"] > 0, r
    print(f"ALL OK: PowerEngine 自测通过 | 累计 {eng.energy_wh/1000:.3f} kWh "
          f"| 峰谷电费 ¥{flat_only:.2f} | 月省(关机+电源) ¥{r['sa']+r['sb']:.1f}")
