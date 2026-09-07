# -*- coding: utf-8 -*-
"""v18.44 改造脚本：主界面四个单元 -> 独立任务栏窗口。用法：python _patch_v1844.py [--check]"""
import io
import sys

P = "main.py"
CHECK = "--check" in sys.argv
s = io.open(P, encoding="utf-8", newline="").read()
s = s.replace("\r\n", "\n")
n = 0


def rep(old, new, cnt=1):
    global s, n
    got = s.count(old)
    tag = old.strip().split("\n")[0][:64]
    if CHECK:
        print("  [%s] %d/%d  %s" % ("OK" if got == cnt else "XX", got, cnt, tag))
        return
    assert got == cnt, "锚点命中 %d 次（应为 %d）: %s" % (got, cnt, tag)
    s = s.replace(old, new)
    n += 1


# ---- 1) 版本号（幂等） ----
if 'APP_VERSION = "v18.43"' in s:
    rep('APP_VERSION = "v18.43"', 'APP_VERSION = "v18.44"')
elif CHECK:
    print("  [--] 版本号已为 v18.44，跳过")

# ---- 2) 主窗口尺寸：只剩标题行 + 控制条 ----
rep(
    '        self.resize(1080, 920)  # v18.35 +40px：实时卡片（含硬件信息块）sizeHint 需要',
    '        self.resize(1040, 190)  # v18.44 四个单元移出为独立窗口，主窗只剩标题+控制条'
)

# ---- 3) _build_ui：四个卡片改为独立面板窗口 ----
rep(
    """        # 三大实时读数 + 倒计时
        page.addWidget(self._live_card())

        # 图表 + 明细
        mid = QHBoxLayout()
        mid.setSpacing(14)
        mid.addWidget(self._chart_card(), 3)
        mid.addWidget(self._breakdown_card(), 2)
        page.addLayout(mid, 1)

        # 控制条
        page.addWidget(self._control_bar())
""",
    """        # v18.44 实时读数 / 功耗曲线 / 功耗构成 / 硬件信息 四个单元不再内嵌，
        # 各自成为独立顶层窗口（可单独最小化到任务栏），见 _make_panels()。
        self._make_panels()

        # 控制条（含四个面板窗口的显隐开关）
        page.addWidget(self._control_bar())

    # ---------------- v18.44 独立面板窗口 ----------------
    def _make_panels(self):
        \"\"\"把四个卡片 reparent 到 _PanelWindow 顶层窗口。

        reparent 后 MainWindow 的引用（self.wall_big / self.table / self.chart_view /
        self.sysinfo_view …）全部保持不变，采样刷新照旧写进这些控件，
        Qt 会把重绘投递到新的父窗口。
        \"\"\"
        self.panels = {}
        builders = {
            "monitor":   lambda: self._live_card(),       # 硬件监测     720x260
            "sysinfo":   lambda: self._sysinfo_panel(),   # 硬件信息     500x570
            "breakdown": lambda: self._breakdown_card(),  # 功耗结构     500x570
            "detail":    lambda: self._chart_card(),      # 管理功耗明细 1160x710
        }
        for key, title, w, h in _PANEL_SPEC:
            pw = _PanelWindow(key, title, w, h, builders[key]())
            pw._on_hidden = self._on_panel_hidden
            self.panels[key] = pw

    def _on_panel_hidden(self, key):
        \"\"\"面板被关闭（实为隐藏）时，同步控制条按钮的勾选状态。\"\"\"
        b = getattr(self, "_panel_btns", {}).get(key)
        if b is not None and b.isChecked():
            try:
                b.blockSignals(True)
                b.setChecked(False)
            finally:
                b.blockSignals(False)

    def _toggle_panel(self, key, on):
        pw = getattr(self, "panels", {}).get(key)
        if pw is None:
            return
        pw.setVisible(bool(on))
        if on:
            pw.showNormal()      # 从任务栏恢复
            pw.raise_()
            pw.activateWindow()
""")

# ---- 4) _live_card：硬件信息不再内嵌（已独立成窗） ----
rep(
    """        # v18.34 横排：左侧读数网格 + 右侧硬件信息块（原最左侧栏整体并入，
        # 填补 v18.5 删除本机配置卡片后留下的右侧空白）。
        row = QHBoxLayout(); row.setSpacing(16)
        outer.addLayout(row, 1)
        # v18.35 读数 2 行 x 3 列（旧 4+2 布局第二行右侧两个格子是空的，
        # 用户截图反馈的空白）。列更宽（~200px），大号数字不再拥挤。
        grid = QGridLayout(); grid.setHorizontalSpacing(16); grid.setVerticalSpacing(10)
        for _c3 in range(3):
            grid.setColumnStretch(_c3, 1)   # v18.35 三列均分，避免某列被文本顶宽
        row.addLayout(grid, 3)
        row.addWidget(self._sysinfo_panel(), 2)
""",
    """        row = QHBoxLayout(); row.setSpacing(16)
        outer.addLayout(row, 1)
        # v18.44 硬件信息块已拆出为独立窗口，此处只剩读数网格，独占整行。
        # 读数 2 行 x 3 列（v18.35：旧 4+2 布局第二行右侧两个格子是空的），
        # 列更宽（~200px），大号数字不再拥挤。
        grid = QGridLayout(); grid.setHorizontalSpacing(16); grid.setVerticalSpacing(10)
        for _c3 in range(3):
            grid.setColumnStretch(_c3, 1)   # v18.35 三列均分，避免某列被文本顶宽
        row.addLayout(grid, 1)
""")

# ---- 5a) _control_bar：外层改 QVBoxLayout ----
rep(
    """        c = QWidget(); self._card(c)
        lay = QHBoxLayout(c); lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        # v18：始终监测，移除「开始监测」按钮""",
    """        c = QWidget(); self._card(c)
        outer = QVBoxLayout(c); outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        lay = QHBoxLayout(); lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        # v18：始终监测，移除「开始监测」按钮""")

# ---- 5b) _control_bar：追加面板显隐行 ----
rep(
    """        for _i, _b in enumerate(_btns):
            grid.addWidget(_b, 0, _i)
            grid.setColumnStretch(_i, 1)
            _b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay.addLayout(grid, 1)
        return c""",
    """        for _i, _b in enumerate(_btns):
            grid.addWidget(_b, 0, _i)
            grid.setColumnStretch(_i, 1)
            _b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer.addLayout(grid, 1)
        # v18.44 面板行：四个独立窗口的显示 / 隐藏
        prow = QHBoxLayout(); prow.setContentsMargins(0, 0, 0, 0)
        prow.setSpacing(8)
        tip = QLabel("面板窗口：")
        tip.setStyleSheet("font-size:12px;color:#6b7488;")
        tip.setMinimumWidth(1)
        prow.addWidget(tip)
        self._panel_btns = {}
        for key, title, _w, _h in _PANEL_SPEC:
            b = QPushButton(title)
            b.setObjectName("ghost")
            b.setCheckable(True)
            b.setChecked(True)
            b.setMinimumWidth(96)
            b.setToolTip(
                "显示 / 隐藏「%s」独立窗口。\\n"
                "它是独立顶层窗口：可单独最小化到任务栏、单独拖动摆放。\\n"
                "（关闭按钮同样只是隐藏，不会停掉后台采样）" % title)
            b.toggled.connect(lambda on, k=key: self._toggle_panel(k, on))
            self._panel_btns[key] = b
            prow.addWidget(b)
        prow.addStretch(1)
        self.btn_panels_all = QPushButton("全部显示")
        self.btn_panels_all.setObjectName("ghost")
        self.btn_panels_all.setMinimumWidth(88)
        self.btn_panels_all.setToolTip("把四个面板窗口全部恢复显示")
        self.btn_panels_all.clicked.connect(self._show_all_panels)
        prow.addWidget(self.btn_panels_all)
        outer.addLayout(prow)
        return c

    def _show_all_panels(self):
        \"\"\"恢复显示四个面板窗口，并同步按钮勾选状态。\"\"\"
        btns = getattr(self, "_panel_btns", None) or {}
        for key, b in btns.items():
            if not b.isChecked():
                b.setChecked(True)      # toggled 信号会触发 _toggle_panel
        for pw in (getattr(self, "panels", None) or {}).values():
            pw.showNormal()
            pw.raise_()
        return""")

# ---- 6) __init__：默认显示四个面板窗口 ----
rep(
    """        self._setup_tray()
        # v18.8 迷你悬浮窗：按会话恢复显示与位置""",
    """        self._setup_tray()
        # v18.44 四个面板窗口默认全部显示（用户可各自最小化到任务栏）
        for _pw in (getattr(self, "panels", None) or {}).values():
            try:
                _pw.show()
            except Exception:
                _log.exception("面板窗口显示失败")
        # v18.8 迷你悬浮窗：按会话恢复显示与位置""")

if CHECK:
    print("\n[CHECK DONE] 未写入文件")
else:
    io.open(P, "w", encoding="utf-8", newline="").write(s)
    print("PATCH OK, %d replacements" % n)
