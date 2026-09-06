"""UI 槽函数冒烟测试（v18.30 新增）。

背景
----
v18.30 之前，点击「历史趋势」毫无反应：open_history() 里对 QBarSeries 调用了
`setColor()`（PySide6 中该方法只存在于 QBarSet），抛 AttributeError；而程序以
`--windowed` 打包、没有控制台，异常被静默吞掉，用户侧只看到「点了没反应」。

本测试逐个调用所有弹窗类槽函数，任何异常都会 Fail，防止同类问题回归。

用法
----
    python tests/test_ui_slots.py                  # 单进程跑全部
    python tests/test_ui_slots.py open_history     # 只跑指定槽（便于用外部 timeout 隔离定位）

说明
----
- 用 offscreen 平台运行：QT_QPA_PLATFORM=offscreen
- 模态 exec() 与文件对话框被替换为立即返回，避免测试阻塞
- _redetect_hardware 依赖 WMI/本机硬件，耗时较长，默认跳过（--all 时包含）
"""
import os
import sys
import json
import time
import tempfile
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import (QApplication, QDialog, QMessageBox,
                               QFileDialog, QInputDialog)

import main as M


# --- 去模态化：exec() 立即返回，文件/输入对话框返回空（走「取消」分支）---
QDialog.exec = lambda self: 1
QMessageBox.exec = lambda self: 1
# 注意：QMessageBox.information/critical 等是 C++ 静态方法，内部走的是 C++ 的
# exec()，patch 上面的 exec 拦不住，必须连静态方法一起替换，否则测试会真起模态循环卡死。
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
QInputDialog.getText = staticmethod(lambda *a, **k: ("", False))
QInputDialog.getDouble = staticmethod(lambda *a, **k: (0.0, False))

SLOTS = [
    "open_history", "open_hourly", "open_apps", "open_compare", "open_sim",
    "open_settings", "export_csv", "export_report", "_save_settings_panel",
]
SLOW_SLOTS = ["_redetect_hardware"]


def _seed_history(path: str):
    hist = [
        {"date": "2026-09-01 10:00", "ts": 1, "duration_h": 2.0, "kwh": 0.5,
         "cost": 0.28, "avg_w": 250.0, "peak_w": 300.0, "mode": "单一", "idle_kwh": 0.05},
        {"date": "2026-09-02 11:00", "ts": 2, "duration_h": 3.0, "kwh": 0.9,
         "cost": 0.50, "avg_w": 300.0, "peak_w": 400.0, "mode": "峰谷", "idle_kwh": 0.10},
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)


def _make_window(tmp: str):
    M.HISTORY_FILE = os.path.join(tmp, "history.json")
    M.SESSION_FILE = os.path.join(tmp, "session.json")
    M.BASE_DIR = tmp
    _seed_history(M.HISTORY_FILE)
    w = M.MainWindow()
    w.hourly = {"2026-09-01 10": [300.0, 3], "2026-09-01 11": [420.0, 3]}
    w.period_energy_wh = {"谷": 30.0, "平": 50.0, "峰": 80.0}
    w.app_cpu_wh = {"chrome": 1.2, "python": 0.4}
    w.energy_wh = 500.0
    w.running_elapsed_ms = 3_600_000.0
    w.cur = {"cpu_load": 20.0, "gpu_power": 30.0, "gpu_valid": True,
             "wall": 120.0, "sys": 100.0, "breakdown": {"CPU": 45.0, "GPU": 30.0}}
    return w


def run_slots(names):
    app = QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp(prefix="pc_power_ui_")
    w = _make_window(tmp)
    failed = []
    for name in names:
        fn = getattr(w, name, None)
        if fn is None:
            print(f"SKIP  {name} (方法不存在)", flush=True)
            continue
        t0 = time.time()
        try:
            fn()
            print(f"OK    {name} ({time.time() - t0:.2f}s)", flush=True)
        except Exception:
            failed.append(name)
            print(f"FAIL  {name}", flush=True)
            traceback.print_exc()
            sys.stdout.flush()
    _shutdown_window(w)
    return failed


def _shutdown_window(w):
    """停掉采样线程，让解释器能干净退出。

    正常产品行为是"关闭窗口 = 最小化到托盘、监测继续"，closeEvent 会忽略关闭，
    采样线程本就不该停。测试进程不走托盘路径，必须显式收尾，否则退出码为 127
    会被 CI 误判成失败（槽函数本身全部通过）。
    """
    try:
        w._force_quit = True
        w.close()
        wk = getattr(w, "worker", None)
        if wk is not None:
            wk.stop()
            if wk.isRunning():
                wk.quit()
                wk.wait(2000)
    except Exception:
        pass


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    include_slow = "--all" in sys.argv
    names = args if args else (SLOTS + (SLOW_SLOTS if include_slow else []))
    failed = run_slots(names)
    if failed:
        print(f"\nUI_SLOT_SMOKE_FAIL: {', '.join(failed)}", flush=True)
        sys.exit(1)
    print(f"\nUI_SLOT_SMOKE_OK ({len(names)} slots)", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
