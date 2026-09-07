# -*- coding: utf-8 -*-
"""v18.46 冒烟：功耗构成新增显卡风扇转速 + 多条内存 / 多块 HDD 逐行展开。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.argv = ["smoke"]

from PySide6.QtWidgets import QApplication          # noqa: E402

QApplication.primaryScreen = lambda *a, **k: None

import main as M                                    # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
w = M.MainWindow()
w.show()
for _ in range(30):
    app.processEvents()

ok = True

# ---- 造数据：2 条内存（8G+16G）、3 块 HDD、1 块 SSD、显卡风扇 1201RPM ----
w._sys_static = {
    "mods": [{"m": "GeIL DDR4", "pn": "GGR48GB", "cap": 8 * (1 << 30), "clk": "3200"},
             {"m": "GeIL DDR4", "pn": "GGR416GB", "cap": 16 * (1 << 30), "clk": "3200"}],
    "disks": [{"model": "ST1000LM035", "media": "HDD", "sizeGB": 1000, "letters": "D:"},
              {"model": "WD5000AAKX", "media": "HDD", "sizeGB": 500, "letters": "E:"},
              {"model": "TOSHIBA DT01", "media": "HDD", "sizeGB": 2000, "letters": "F:"},
              {"model": "Samsung 980", "media": "SSD", "sizeGB": 500, "letters": "C:"}],
}
w._sys_dyn = {
    "fans": [("Fan #1", 1937), ("GPU", 1201)],
    "fan_kinds": ["board", "gpu"],
    "sensor_temps": {"cpu": 66.9, "motherboard": 26.0, "gpu": 41.0},
    "sensor_ready": True,
    "cpu_temp": 66.9,
    "gpu_temp": 41.0,
    "disk_temps": {"ST1000LM035": 38.0, "WD5000AAKX": 41.0, "TOSHIBA DT01": 36.0,
                   "Samsung 980": 45.0},
    "ram_pct": 62.0,
}
w.cur = {
    "breakdown": {
        "CPU": 42.0, "GPU": 35.0, "内存": 6.0, "风扇": 8.0,
        "SSD": 3.0, "HDD": 18.0, "主板/芯片组": 15.0, "显示器": 30.0, "外设": 5.0,
    },
    "cpu_load": 12.0,
}

w._refresh_breakdown()
for _ in range(10):
    app.processEvents()

t = w.table
rows = []
for r in range(t.rowCount()):
    rows.append((
        t.item(r, 0).text() if t.item(r, 0) else "",
        t.item(r, 2).text() if t.item(r, 2) else "",
        t.item(r, 3).text() if t.item(r, 3) else "",
    ))

print("%-14s %8s  %s" % ("部件", "功耗 W", "温度 / 转速"))
for n, v, tp in rows:
    print("%-14s %8s  %s" % (n, v, tp))

names = [n for n, _v, _t in rows]
expect = ["CPU", "GPU", "内存1", "内存2", "风扇", "SSD", "HDD1", "HDD2", "HDD3"]
for e in expect:
    if e not in names:
        print("!! 缺少行：", e)
        ok = False

# 1) 合计守恒（展开后与原始 breakdown 总和一致）
orig = sum(w.cur["breakdown"].values())
now = 0.0
for n, v, _t in rows:
    try:
        now += float(v)
    except ValueError:
        pass
print("\n合计: 原始 %.1f W -> 展开后 %.1f W" % (orig, now))
if abs(orig - now) > 0.15:
    print("!! 展开后合计不守恒")
    ok = False

# 2) 内存按容量比例分摊（8G:16G → 2.0 / 4.0）
mem = {n: v for n, v, _t in rows if n.startswith("内存")}
print("内存分摊:", mem)
if mem.get("内存1") != "2.0" or mem.get("内存2") != "4.0":
    print("!! 内存未按容量比例分摊")
    ok = False

# 3) 显卡风扇转速出现在 GPU 行
gpu_row = [tp for n, _v, tp in rows if n == "GPU"]
print("GPU 行温度/转速:", gpu_row)
if not gpu_row or "1201" not in gpu_row[0]:
    print("!! GPU 行未显示显卡风扇转速")
    ok = False

# 4) 风扇行只显示机箱风扇（1937），不应被显卡风扇 1201 顶掉
fan_row = [tp for n, _v, tp in rows if n == "风扇"]
print("风扇行:", fan_row)
if not fan_row or "1937" not in fan_row[0]:
    print("!! 风扇行未排除显卡风扇")
    ok = False

# 5) 逐块 HDD 温度（HDD1=38 / HDD2=41 / HDD3=36）
hdd = {n: tp for n, _v, tp in rows if n.startswith("HDD")}
print("HDD 逐盘温度:", hdd)
if hdd.get("HDD1") != "38°" or hdd.get("HDD2") != "41°" or hdd.get("HDD3") != "36°":
    print("!! HDD 未按块取对应温度")
    ok = False

# 6) 悬浮窗构成同样展开
try:
    m = M.MiniOverlay()
    items = w._bd_items(w.cur["breakdown"])
    m.set_breakdown(dict(items), {}, {}, {})
    for _ in range(5):
        app.processEvents()
    labels = []
    for rw in m._bd_row_widgets:
        lbs = rw.findChildren(type(m.lbl_w))
        if lbs:
            labels.append(lbs[0].text())
    print("悬浮窗行:", labels[:6], "…共", len(labels))
    if "HDD1" not in labels or "内存1" not in labels:
        print("!! 悬浮窗未展开")
        ok = False
except Exception as e:
    print("!! 悬浮窗异常:", e)
    ok = False

print("\nRESULT:", "OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
