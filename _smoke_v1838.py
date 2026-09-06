# -*- coding: utf-8 -*-
"""v18.38 冒烟：GPU 真实占用率 + 内存温度说明 + 传感器窗口「显示全部」。"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QDialog, QTableWidget, QCheckBox

import main
import hardware as H

app = QApplication(sys.argv)
w = main.MainWindow()

# 真实采样（nvidia-smi 常驻流）
_ps = H.PersistentSampler(interval_ms=2000, gpu_nvidia=True)
_ps.start()
import time
time.sleep(5)
_d = _ps.sample()
_ps.stop()
w.cur = {"cpu_load": _d["cpu_load"], "gpu_power": _d["gpu_power"],
         "gpu_util": _d["gpu_util"], "gpu_valid": _d["gpu_valid"]}
w._sys_dyn = {"sensor_ready": True,
              "sensor_temps": H.sensor_snapshot_cached(force=True)[1]}

print("[采样] gpu_power=%s W  gpu_util(真实)=%s %%  TDP=%s W"
      % (_d["gpu_power"], _d["gpu_util"], getattr(w.model, "gpu_tdp", "?")))
_u = w._util_pct("GPU")
print("[GPU 使用率] 显示值 = %s %%   来源: %s" % (_u, w._gpu_util_src))
print("[GPU tooltip] %s" % w._util_tip("GPU").replace("\n", " | "))
try:
    _old = (_d["gpu_power"] or 0) / float(getattr(w.model, "gpu_tdp", 0) or 1) * 100
    print("[旧算法] 功耗/TDP = %.0f %%  → 偏差 %+.0f 个百分点" % (_old, _old - (_u or 0)))
except Exception:
    pass

print()
print("[内存温度 tooltip]")
for line in w._temp_tooltip("内存").splitlines():
    print("   ", line)

# 传感器窗口：默认视图 + 勾选「显示全部」后应能看到内存 SPD 时序
_rows = {}


def _grab(dlg):
    tw = dlg.findChild(QTableWidget)
    return [tw.item(r, 0).text().strip() for r in range(tw.rowCount())]


_orig = QDialog.exec
_res = {}


def _fake(self):
    _res["default"] = _grab(self)
    ck = self.findChild(QCheckBox)
    ck.setChecked(True)
    _res["all"] = _grab(self)
    return 0


QDialog.exec = _fake
try:
    w.open_sensors()
finally:
    QDialog.exec = _orig

print()
print("[传感器] 默认 %d 行；显示全部 %d 行" % (len(_res["default"]), len(_res["all"])))
print("[默认视图含温度/风扇]", [x for x in _res["default"] if "Temperature #1" in x or "GPU Core" in x][:3])
print("[全部视图可见内存SPD]", [x for x in _res["all"] if "tCK" in x or "Capacity" in x][:3])

ok = (len(_res["all"]) > len(_res["default"])) and any("Timing" in "" or "tCK" in x for x in _res["all"])
print()
print("V1838_SMOKE_OK" if ok else "V1838_SMOKE_FAIL")
sys.exit(0 if ok else 1)
