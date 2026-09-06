# -*- coding: utf-8 -*-
"""v18.29+ W3 降级账本守卫测试。

确保：
  1) 启动时 _degraded 为空集合；
  2) _mark_degraded 能登记并在 _degraded_summary 中可见；
  3) 报告 HTML 在降级时包含「降级运行」提示，未降级时不包含；
  4) _clear_degraded 能清除。

运行：
    python tests/test_degraded.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

import main
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)
w = main.MainWindow()


def test_degraded_starts_empty():
    assert isinstance(w._degraded, set)
    assert w._degraded == set(), "启动不应有任何降级项"


def test_mark_and_summary():
    w._mark_degraded("session_save", "会话保存失败·配置可能未持久化")
    assert "session_save" in w._degraded
    s = w._degraded_summary()
    assert "会话保存失败" in s


def test_report_shows_banner_when_degraded():
    html = w._build_report_html()
    assert "降级运行" in html, "降级状态必须在报告中可见"
    w._clear_degraded("session_save")
    assert "session_save" not in w._degraded
    html2 = w._build_report_html()
    assert "降级运行" not in html2, "清除降级后报告不应再展示降级横幅"


def test_report_no_banner_when_clean():
    # 全新实例（上面已 clear 干净），报告不应含降级横幅
    assert w._degraded_summary() == ""
    assert "降级运行" not in w._build_report_html()


if __name__ == "__main__":
    test_degraded_starts_empty()
    test_mark_and_summary()
    test_report_shows_banner_when_degraded()
    test_report_no_banner_when_clean()
    print("ALL OK: W3 降级账本测试通过")
    # 采样线程(SampleWorker)会阻止解释器干净退出——正常产品行为就是"关闭=最小化
    # 到托盘、监测继续"，线程本就不该停。测试进程必须显式强制退出，否则退出码
    # 为 127，会让 CI 误判成测试失败（测试内容本身是通过的）。
    try:
        w._force_quit = True
        w.close()
        _wk = getattr(w, "worker", None)
        if _wk is not None:
            _wk.stop()
            if _wk.isRunning():
                _wk.quit()
                _wk.wait(2000)
    except Exception:
        pass
    sys.exit(0)
