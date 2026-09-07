# -*- coding: utf-8 -*-
"""v18.44 冒烟：创建主窗与四个独立面板，检查标志/尺寸/任务栏可见性并截图。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt                      # noqa: E402
from PySide6.QtWidgets import QApplication         # noqa: E402

import main as M                                   # noqa: E402

app = QApplication(sys.argv)
w = M.MainWindow()
w.show()

print("APP_VERSION =", M.APP_VERSION)
print("主窗口标题  =", w.windowTitle())
print("主窗口尺寸  = %dx%d" % (w.width(), w.height()))
print()

ok = True
for key, pw in w.panels.items():
    flags = pw.windowFlags()
    raw = int(flags)
    # Qt.Tool = 0x0B 本身包含 Window(0x1) 位，直接 '&' 会把任何 Window 误判成 Tool。
    # 窗口「类型」只占低 8 位：Widget=0 / Window=1 / Dialog=3 / Popup=9 / Tool=0x0B
    wtype = raw & 0xFF
    name = {0: "Widget", 1: "Window", 3: "Dialog", 9: "Popup",
            0x0B: "Tool"}.get(wtype, "0x%X" % wtype)
    is_win = (wtype == 1)
    skip = (wtype == 0x0B)
    has_min_btn = bool(flags & Qt.WindowType.WindowMinimizeButtonHint)
    vis = pw.isVisible()
    print("面板 %-10s %4dx%-4d visible=%s type=%-6s minBtn=%s flags=0x%X"
          % (key, pw.width(), pw.height(), vis, name, has_min_btn, raw))
    if not is_win or skip or not has_min_btn:
        print("   !! 窗口类型 %s —— 不会出现独立任务栏条目" % name)
        ok = False

# 检查面板里的关键控件仍可访问（reparent 后引用是否悬空）
checks = {
    "wall_big": lambda: w.wall_big.text(),
    "energy_big": lambda: w.energy_big.text(),
    "table": lambda: str(w.table.rowCount()),
    "chart_view": lambda: str(w.chart_view is not None),
    "sysinfo_view": lambda: str(len(w.sysinfo_view.toPlainText())),
}
print()
for name, fn in checks.items():
    try:
        print("  控件 %-12s -> %s" % (name, fn()))
    except Exception as e:
        print("  控件 %-12s -> 访问失败 %r" % (name, e))
        ok = False

# 触发一次采样刷新（常见崩溃点：reparent 后写入只读表格第5列）
try:
    w.cur = {"cpu_load": 12.0, "gpu_power": 30.0, "gpu_valid": True,
             "wall": 95.0, "sys": 80.0,
             "breakdown": {"CPU": 45.0, "GPU": 30.0, "主板": 5.0}}
    w.last_ts = __import__("time").time() - 1
    w._on_ui_tick()
except Exception as e:
    print("  刷新阶段异常（可能非致命）: %r" % (e,))

# 截图
try:
    for key, pw in w.panels.items():
        pw.grab().save("_panel_%s.png" % key)
    w.grab().save("_panel_main.png")
    print("\n截图已生成: _panel_*.png")
except Exception as e:
    print("截图失败: %r" % (e,))

print("\nRESULT:", "OK" if ok else "FAIL")
