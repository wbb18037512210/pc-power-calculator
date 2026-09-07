# -*- coding: utf-8 -*-
"""v18.45 冒烟：单一主界面 + 卡片指定尺寸 + 分隔条可鼠标拖动。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.argv = ["smoke"]

from PySide6.QtWidgets import QApplication, QSplitter            # noqa: E402
from PySide6.QtCore import Qt, QPoint                            # noqa: E402
from PySide6.QtTest import QTest                                 # noqa: E402

# 离屏平台屏幕只有 640x480 会把窗口夹小，冒烟时取消限制以验证真实目标尺寸
QApplication.primaryScreen = lambda *a, **k: None

import main as M                                                 # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)
w = M.MainWindow()
w.show()
for _ in range(40):
    app.processEvents()

ok = True

# 1) 单一主界面：不应再有 4 个独立顶层窗口
tops_v = [t for t in app.topLevelWidgets() if t.isWindow() and t.isVisible()]
print("可见顶层窗口 %d 个" % len(tops_v))
for t in tops_v:
    print("   -", (t.windowTitle() or t.objectName())[:40])
if len(tops_v) > 3:
    print("   !! 顶层窗口过多，疑似又拆成了独立窗口")
    ok = False

# 2) 分栏结构与卡片尺寸
sp = getattr(w, "_split_main", None)
if not isinstance(sp, QSplitter):
    print("RESULT: FAIL（_split_main 不是 QSplitter）")
    sys.exit(1)

top = sp.widget(0)
left = top.widget(0)
cards = [
    ("硬件监测(指定 720x260)", left.widget(0), 720, 260),
    ("硬件信息(自动缩小)",     left.widget(1), None, None),
    ("功耗结构(指定 500x570)", top.widget(1), 500, 570),
    ("管理功耗明细(自动缩小)", sp.widget(1), None, None),
]
print("\n窗口尺寸: %dx%d" % (w.width(), w.height()))
print("%-26s %10s   %s" % ("卡片", "实际尺寸", "期望"))
for name, wd, ew, eh in cards:
    exp = ""
    if ew:
        dw, dh = wd.width() - ew, wd.height() - eh
        good = abs(dw) <= 3 and abs(dh) <= 3
        exp = "%dx%d -> %s" % (ew, eh, "OK" if good else "偏差 %+d/%+d" % (dw, dh))
        if not good:
            ok = False
    print("%-26s %10s   %s" % (name, "%dx%d" % (wd.width(), wd.height()), exp))


# 3) 真实鼠标拖动分隔条
def drag(splitter, delta):
    """拖动 splitter 的分隔条，返回 (拖前, 拖后) 的第二个 widget 尺寸。

    两个要点（都是实测踩出来的）：
    - 2 个 widget 的 splitter，handle 索引从 1 起，handle(0) 是无效的
    - 每个鼠标事件后都要 processEvents，否则 splitter 收不到
    """
    h = splitter.handle(1)
    if h is None:
        return None
    horiz = splitter.orientation() == Qt.Orientation.Horizontal
    wd = splitter.widget(1)

    def size():
        return wd.width() if horiz else wd.height()

    before = size()
    p0 = QPoint(h.width() // 2, h.height() // 2)
    p1 = QPoint(p0.x() + (delta if horiz else 0),
                p0.y() + (0 if horiz else delta))
    QTest.mousePress(h, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, p0)
    app.processEvents()
    QTest.mouseMove(h, p1)
    app.processEvents()
    QTest.mouseRelease(h, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier, p1)
    for _ in range(6):
        app.processEvents()
    return before, size()


print("\n拖动测试（QTest 真实鼠标事件）：")
cases = [("上部横向(改功耗结构宽)", top, 80),
         ("主纵向(改明细高)", sp, 80),
         ("左列纵向(改硬件信息高)", left, 60)]
for label, spt, d in cases:
    r = drag(spt, d)
    if r is None:
        print("   %-24s !! 无 handle" % label)
        ok = False
        continue
    b, a = r
    changed = (a != b)
    print("   %-24s %d -> %d  变化=%+d %s"
          % (label, b, a, a - b, "OK" if changed else "!! 无效"))
    if not changed:
        ok = False

# 4) 分隔条状态可存取 / 还原
try:
    st = w._session_frames()
    assert set(st) == {"main", "top", "left"}, list(st)
    w._restore_split_sizes(st)
    print("\n分隔条状态: %s（还原 OK）" % sorted(st))
except Exception as e:
    print("!! 分隔条状态异常:", e)
    ok = False

print("\nRESULT:", "OK" if ok else "FAIL")
sys.exit(0 if ok else 1)
