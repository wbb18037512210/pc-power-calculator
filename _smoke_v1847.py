# -*- coding: utf-8 -*-
"""v18.47 冒烟：新增「开机以来」——读取开机时长并估算开机至今电费。"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.argv = ["smoke"]

from PySide6.QtWidgets import QApplication          # noqa: E402

QApplication.primaryScreen = lambda *a, **k: None

import main as M                                    # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
w = M.MainWindow()
w.show()
for _ in range(20):
    app.processEvents()

ok = True

# ---- 造数据：开机 3天5时17分 前，插座功耗 120W，单一电价 0.56 元/度 ----
boot_ts = time.time() - (3 * 86400 + 5 * 3600 + 17 * 60)   # 3天5时17分
w._sys_dyn = {
    "boot": boot_ts,
    "cpu_temp": 66.9, "gpu_temp": 41.0,
    "sensor_temps": {"cpu": 66.9, "motherboard": 26.0, "gpu": 41.0},
    "fans": [("Fan #1", 1937), ("GPU", 1201)],
    "fan_kinds": ["board", "gpu"],
    "ram_total": 32 * (1 << 30), "ram_used": 12 * (1 << 30), "ram_pct": 37.0,
}
w.cur = {
    "wall": 120.0, "sys": 100.0, "cpu_load": 12.0,
    "gpu_valid": False, "gpu_power": None,
    "breakdown": {"CPU": 42.0, "GPU": 35.0, "内存": 6.0, "风扇": 8.0,
                  "SSD": 3.0, "HDD": 18.0, "主板/芯片组": 15.0, "显示器": 30.0, "外设": 5.0},
    "display_on": True, "psu_eff": 0.85,
}
# 让 blended_rate 取到 0.56（单一价）
w.energy_wh = 1000.0
w.rate = 0.56
w.price_mode = "单一"
w.running_elapsed_ms = 3600 * 1000.0
w.budget_kwh = 0.0
w.budget_cost = 0.0
w.idle_energy_wh = 0.0
w.psu_rating_w = 0.0
w.display_on = True

# ---- 直接校验核心方法 ----
bi = w._boot_info()
print("boot_info:", bi)
if bi is None:
    print("!! _boot_info 返回 None")
    ok = False
else:
    dur, cost, up_s = bi
    print("  时长文本:", dur, "| 开机秒数:", up_s, "| 电费 ¥%.2f" % cost)
    if dur != "3天5时17分":
        print("!! 时长格式化错误，期望 3天5时17分，实得", dur)
        ok = False
    # 期望：120W × 77.2833h ÷1000 ×0.56 ≈ 5.19
    if abs(cost - 5.19) > 0.05:
        print("!! 电费估算偏差，期望≈5.19，实得 %.2f" % cost)
        ok = False

# _uptime_line 应包含「开机电费」
ul = w._uptime_line()
print("uptime_line:", ul)
if "开机电费" not in ul or "¥" not in ul:
    print("!! _uptime_line 未包含开机电费")
    ok = False

# _fmt_dur 边界
cases = [0, 45, 59 * 60, 3 * 3600 + 5 * 60, 2 * 86400 + 4 * 3600 + 9 * 60]
exp = ["0分", "0分", "59分", "3时05分", "2天4时09分"]
for s, e in zip(cases, exp):
    got = w._fmt_dur(s)
    print("  _fmt_dur(%d) = %s (期望 %s)" % (s, got, e))
    if got != e:
        print("!! _fmt_dur 错误")
        ok = False

# ---- 走真实刷新路径，校验控件被填充 ----
try:
    w._refresh_readout()
    for _ in range(10):
        app.processEvents()
    print("boot_dur 控件:", w.boot_dur.text())
    print("boot_cost 控件:", w.boot_cost.text())
    if w.boot_dur.text() != "3天5时17分":
        print("!! boot_dur 控件文本错误")
        ok = False
    if not w.boot_cost.text().startswith("¥") or "5.19" not in w.boot_cost.text():
        print("!! boot_cost 控件文本错误:", w.boot_cost.text())
        ok = False
except Exception as e:
    import traceback
    traceback.print_exc()
    print("!! _refresh_readout 异常:", e)
    ok = False

# 侧栏 HTML 首行应包含开机以来
try:
    html = w._build_sysinfo_html()
    print("侧栏含『开机以来』:", "开机以来" in html)
    if "开机以来" not in html:
        print("!! 侧栏未包含开机以来")
        ok = False
except Exception as e:
    print("!! _build_sysinfo_html 异常:", e)
    ok = False

print("\nRESULT:", "OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
