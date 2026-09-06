# -*- coding: utf-8 -*-
"""v18.37 冒烟：传感器详情窗口 + 温度列 LHM 参考读数 tooltip（offscreen）。"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QDialog, QTableWidget

import main
import hardware as H

app = QApplication(sys.argv)
w = main.MainWindow()

# 0) 先落地一次真实采样（tooltip 依赖 _sys_dyn 的 sensor_ready）
_f, _t, _r = H.sensor_snapshot_cached(force=True)
w._sys_dyn = {"sensor_ready": _r, "sensor_temps": _t, "fans": _f}
print("[sampling] ready =", _r, "| temps =", _t)

# 1) tooltip：LHM 参考读数
for part in ("CPU", "GPU", "内存", "主板/芯片组", "风扇", "SSD", "HDD", "显示器"):
    tip = w._temp_tooltip(part)
    first = (tip.splitlines() or [""])[0]
    print(f"[tip] {part:<12} -> {first}  ({len(tip.splitlines())} 行)")

# 2) 传感器详情窗口（exec 打桩为 no-op，避免模态阻塞）
_rows = {}
_orig_exec = QDialog.exec


def _fake_exec(self):
    tw = self.findChild(QTableWidget)
    _rows["n"] = tw.rowCount()
    _rows["sample"] = [tw.item(r, 0).text().strip() + " = " +
                       (tw.item(r, 2).text() if tw.item(r, 2) else "")
                       for r in range(min(tw.rowCount(), 12))]
    return 0


QDialog.exec = _fake_exec
try:
    w.open_sensors()
finally:
    QDialog.exec = _orig_exec

print()
print("[sensors] 行数 =", _rows.get("n"))
for line in _rows.get("sample", []):
    print("   ", line)

ok = (_rows.get("n") or 0) > 5
print()
print("SENSOR_DIALOG_OK" if ok else "SENSOR_DIALOG_FAIL")
sys.exit(0 if ok else 1)
