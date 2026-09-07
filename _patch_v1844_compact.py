# -*- coding: utf-8 -*-
"""v18.44 紧凑化：全局收紧卡片内外边距与投影。用法：python _patch_v1844_compact.py [--check]"""
import io
import sys

P = "main.py"
CHECK = "--check" in sys.argv
s = io.open(P, encoding="utf-8", newline="").read()
s = s.replace("\r\n", "\n")
n = 0

# (说明, old, new, 期望次数)  —— -1 表示不限次数
REPS = [
    ("页面外框边距", "page.setContentsMargins(18, 16, 18, 16)",
     "page.setContentsMargins(8, 7, 8, 7)", 1),
    ("页面元素间距", "page.setSpacing(14)", "page.setSpacing(6)", 1),
    ("卡片内容边距", "lay.setContentsMargins(14, 12, 14, 12)",
     "lay.setContentsMargins(6, 5, 6, 5)", -1),
    ("卡片元素间距", "lay.setSpacing(8)", "lay.setSpacing(3)", -1),
    ("实时卡外框", "outer.setContentsMargins(16, 14, 16, 14)",
     "outer.setContentsMargins(8, 6, 8, 6)", -1),
    ("实时卡间距", "outer.setSpacing(8)", "outer.setSpacing(4)", -1),
    ("读数网格间距", "grid.setHorizontalSpacing(16); grid.setVerticalSpacing(10)",
     "grid.setHorizontalSpacing(10); grid.setVerticalSpacing(6)", 1),
    # 主界面卡片没有 18px 阴影（那两处在 MiniOverlay 悬浮窗内，不需收紧）
    ("硬件卡列间距", "lay.setSpacing(22)", "lay.setSpacing(12)", 1),
    ("控制条外框", "outer.setContentsMargins(14, 12, 14, 12)\n        outer.setSpacing(8)",
     "outer.setContentsMargins(7, 6, 7, 6)\n        outer.setSpacing(5)", 1),
]


def apply(desc, old, new, cnt):
    global s, n
    got = s.count(old)
    ok = (got > 0) if cnt == -1 else (got == cnt)
    print("  [%s] %-8s %d 次  %s" % ("OK" if ok else "XX", desc, got, desc))
    if not ok or CHECK:
        return
    s = s.replace(old, new)
    n += 1


for desc, old, new, cnt in REPS:
    apply(desc, old, new, cnt)

if CHECK:
    print("\n[CHECK DONE] 未写入文件")
else:
    io.open(P, "w", encoding="utf-8", newline="").write(s)
    print("PATCH OK, %d 组替换" % n)
