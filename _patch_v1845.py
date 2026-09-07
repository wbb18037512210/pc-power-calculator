# -*- coding: utf-8 -*-
"""v18.45 改造：回退 v18.44 的四窗口拆分，回到单一主界面。

- 硬件监测卡片 720x260、功耗结构卡片 500x570（指定尺寸）
- 其余卡片（硬件信息、管理功耗明细）自动缩小
- 四个卡片全部走 QSplitter —— 拖分隔条即可调整各卡片大小
- 分隔条状态随会话持久化
"""
import io
import os
import sys

P = "main.py"
raw = io.open(P, encoding="utf-8", newline="").read()
CRLF = raw.count("\r\n") > raw.count("\n") - raw.count("\r\n")
s = raw.replace("\r\n", "\n")
orig = s
n = 0


def rep(old, new, cnt=1):
    global s, n
    c = s.count(old)
    assert c == cnt, "锚点命中 %d 次（应为 %d）: %r" % (c, cnt, old[:90])
    s = s.replace(old, new)
    n += 1


def cut(start_marker, end_marker):
    """删除 [start_marker, end_marker) 之间的整段（start_marker 行首起）。"""
    global s, n
    i = s.find(start_marker)
    assert i >= 0, "未找到起始锚点: %r" % start_marker[:60]
    j = s.find(end_marker, i)
    assert j > i, "未找到结束锚点: %r" % end_marker[:60]
    s = s[:i] + s[j:]
    n += 1


# ---- 1) 版本号 v18.44 -> v18.45 ----
rep('APP_VERSION = "v18.44"', 'APP_VERSION = "v18.45"')

# ---- 2) 导入 QSplitter ----
rep("""    QProgressBar, QInputDialog, QTextBrowser, QComboBox, QScrollArea,
    QGraphicsDropShadowEffect,
)""",
    """    QProgressBar, QInputDialog, QTextBrowser, QComboBox, QScrollArea,
    QGraphicsDropShadowEffect, QSplitter,
)""")

# ---- 3) 删除 _PANEL_SPEC 与 _PanelWindow 类 ----
cut("# v18.44 主界面四单元独立化", "class MainWindow(QMainWindow):")

# ---- 4) 主窗口初始尺寸改为按卡片尺寸推算 ----
rep("""        self.resize(1040, 190)  # v18.44 四个单元移出为独立窗口，主窗只剩标题+控制条""",
    """        self.resize(*self._main_window_size())   # v18.45 按指定卡片尺寸推算""")

# ---- 5) _make_panels() 调用 -> 分栏布局 ----
rep("""        # v18.44 实时读数 / 功耗曲线 / 功耗构成 / 硬件信息 四个单元不再内嵌，
        # 各自成为独立顶层窗口（可单独最小化到任务栏），见 _make_panels()。
        self._make_panels()

        # 控制条（含四个面板窗口的显隐开关）
        page.addWidget(self._control_bar())""",
    """        # v18.45 仍是单一主界面：四个卡片用 QSplitter 组织，
        # 拖分隔条即可调整各卡片大小（硬件监测 720x260 / 功耗结构 500x570 为指定尺寸，
        # 硬件信息、管理功耗明细自动缩小占满剩余空间）。
        page.addWidget(self._build_main_split(), 1)

        # 控制条
        page.addWidget(self._control_bar())""")

# ---- 6) 整段替换独立面板窗口方法 -> 分栏实现 ----
cut("    # ---------------- v18.44 独立面板窗口 ----------------",
    "    def _card(self, widget: QWidget):")

SPLIT_CODE = '''    # ---------------- v18.45 主界面分栏（拖动分隔条调整卡片大小） ----------------
    def _main_window_size(self):
        """按指定卡片尺寸推算主窗口大小，并夹进屏幕可用区。

        宽 = 硬件监测 720 + 功耗结构 500 + 边距/分隔条；
        高 = 功耗结构 570 + 底部明细 230 + 标题栏/控制条/边距。
        """
        lw, _lh = UI_CARD_LIVE
        bw, bh = UI_CARD_BREAKDOWN
        w = lw + bw + 60
        h = bh + 230 + 190
        try:
            scr = QApplication.primaryScreen()
            ag = scr.availableGeometry() if scr is not None else None
            if ag is not None:
                w = min(w, max(900, ag.width() - 40))
                h = min(h, max(560, ag.height() - 40))
        except Exception:
            pass
        return (int(w), int(h))

    def _build_main_split(self):
        """v18.45 单一主界面，四个卡片用嵌套 QSplitter 组织。

            VSplit（主）
            ├─ HSplit（上部）
            │   ├─ VSplit（左）: 硬件监测 720x260 / 硬件信息（自动缩小）
            │   └─ 功耗结构 500x570
            └─ 管理功耗明细（自动缩小，底部通栏）

        拖动任意分隔条即改变相邻卡片大小；setChildrenCollapsible(False)
        防止把卡片整个拖没（拖到 0 后拉不回来）。
        """
        _SS = ("QSplitter::handle { background: #dfe4ee; border-radius: 2px; }"
               "QSplitter::handle:hover { background: #2f6bff; }")

        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self._live_card())        # 硬件监测   720x260
        left.addWidget(self._sysinfo_panel())    # 硬件信息   自动缩小
        left.setChildrenCollapsible(False)
        left.setHandleWidth(6)
        left.setStyleSheet(_SS)

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(left)
        top.addWidget(self._breakdown_card())    # 功耗结构   500x570
        top.setChildrenCollapsible(False)
        top.setHandleWidth(6)
        top.setStyleSheet(_SS)

        self._split_main = QSplitter(Qt.Orientation.Vertical)
        self._split_main.addWidget(top)
        self._split_main.addWidget(self._chart_card())   # 管理功耗明细 自动缩小
        self._split_main.setChildrenCollapsible(False)
        self._split_main.setHandleWidth(6)
        self._split_main.setStyleSheet(_SS)
        return self._split_main

    def _reset_split_sizes(self):
        """按 UI_CARD_* 指定尺寸给分隔条分配初始空间，其余自动缩小。"""
        try:
            sp = getattr(self, "_split_main", None)
            if sp is None:
                return
            top = sp.widget(0)
            left = top.widget(0)
            _lw, lh = UI_CARD_LIVE
            bw, _bh = UI_CARD_BREAKDOWN
            # 左列：硬件监测占 lh 高，余下给硬件信息
            lh_total = left.height() or (lh + 300)
            left.setSizes([min(lh, max(80, lh_total - 120)),
                           max(120, lh_total - lh)])
            # 上部横向：功耗结构占 bw 宽，余下给左列
            w_total = top.width() or (720 + bw)
            left_w = max(320, w_total - bw)
            top.setSizes([left_w, max(240, w_total - left_w)])
            # 主纵向：底部明细 230，其余给上部
            h_total = sp.height() or (lh + 570 + 230)
            top_h = max(300, h_total - 230)
            sp.setSizes([top_h, max(120, h_total - top_h)])
        except Exception:
            _log.exception("分隔条初始尺寸分配失败")

'''

rep("    def _card(self, widget: QWidget):", SPLIT_CODE + "    def _card(self, widget: QWidget):")

# ---- 7) 控制条：移除面板显隐行 ----
rep('''        # v18.44 面板行：四个独立窗口的显示 / 隐藏
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
        return c''', '''        return c''')

# ---- 8) 删除 _show_all_panels ----
rep('''    def _show_all_panels(self):
        """恢复显示四个面板窗口，并同步按钮勾选状态。"""
        btns = getattr(self, "_panel_btns", None) or {}
        for key, b in btns.items():
            if not b.isChecked():
                b.setChecked(True)      # toggled 信号会触发 _toggle_panel
        for pw in (getattr(self, "panels", None) or {}).values():
            pw.showNormal()
            pw.raise_()
        return

''', '')

# ---- 9) 托盘：移除「显示全部面板窗口」 ----
rep('''        # v18.10 手动生成昨日日报
        a_panel_all = menu.addAction("显示全部面板窗口")   # v18.44
        a_panel_all.triggered.connect(lambda: self._show_all_panels())
        a_daily''', '''        # v18.10 手动生成昨日日报
        a_daily''')

# ---- 10) 会话持久化：frames -> 分隔条状态 ----
rep('''            # v18.44 面板窗口几何（位置/尺寸）
            data["frames"] = self._session_frames()''',
    '''            # v18.45 分栏布局（用户拖动出的各卡片大小）
            data["split_state"] = self._session_frames()''')

rep('''    def _session_frames(self):
        """v18.44 收集四个面板窗口的几何 [x,y,w,h]，越界值钳制到合理范围。"""
        out = {}
        for k, win in (getattr(self, "panels", None) or {}).items():
            try:
                g = win.geometry()
                out[k] = [max(0, int(g.x())), max(0, int(g.y())),
                          max(120, int(g.width())), max(80, int(g.height()))]
            except Exception:
                continue
        return out

    def _apply_session_panels(self, frames):
        """v18.44 恢复上次的面板位置尺寸；缺失或损坏则回退默认摆放。"""
        panels = getattr(self, "panels", None) or {}
        if not isinstance(frames, dict) or not panels:
            return
        ok = 0
        for k, geo in frames.items():
            win = panels.get(k)
            if not (win and isinstance(geo, (list, tuple)) and len(geo) == 4):
                continue
            try:
                scr = win.screen() or QApplication.primaryScreen()
                ag = scr.availableGeometry() if scr is not None else None
                x, y, w, h = (int(geo[0]), int(geo[1]), int(geo[2]), int(geo[3]))
                # 完全跑出屏幕的历史坐标直接丢弃，避免"窗口失踪"
                if ag is not None and x > ag.right() - 40 or y > ag.bottom() - 40:
                    continue
                win.setGeometry(x, y, max(120, w), max(80, h))
                ok += 1
            except Exception:
                continue
        if not ok:
            self._tile_panels()   # 无可用历史布局：回到默认错开摆放
''',
    '''    def _session_frames(self):
        """v18.45 收集各分隔条状态（base64），下次启动还原卡片大小。"""
        out = {}
        sp = getattr(self, "_split_main", None)
        if sp is None:
            return out
        try:
            for name, w in (("main", sp), ("top", sp.widget(0)),
                            ("left", sp.widget(0).widget(0))):
                if w is not None:
                    out[name] = bytes(w.saveState().toBase64()).decode("ascii")
        except Exception:
            _log.exception("分隔条状态收集失败")
        return out

    def _apply_session_panels(self, frames):
        """v18.45 还原上次拖动出的卡片大小；缺失/损坏则回退默认尺寸。

        restoreState 在窗口尚未 show 时算不出geometry，故延迟一帧执行。
        """
        sp = getattr(self, "_split_main", None)
        if sp is None:
            return
        if not isinstance(frames, dict) or not frames:
            QTimer.singleShot(0, self._reset_split_sizes)
            return
        state = dict(frames)
        QTimer.singleShot(0, lambda: self._restore_split_sizes(state))

    def _restore_split_sizes(self, state):
        ok = False
        try:
            import base64
            sp = getattr(self, "_split_main", None)
            if sp is not None:
                for name, w in (("main", sp), ("top", sp.widget(0)),
                                ("left", sp.widget(0).widget(0))):
                    v = state.get(name)
                    if isinstance(v, str) and v and w is not None:
                        w.restoreState(base64.b64decode(v))
                        ok = True
        except Exception:
            _log.warning("分隔条状态还原失败，改用默认尺寸", exc_info=True)
            ok = False
        if not ok:
            self._reset_split_sizes()
''')

rep('''        self._apply_session_panels(d.get("frames") or {})''',
    '''        self._apply_session_panels(d.get("split_state") or {})''')

# ---- 11) UI 尺寸常量：改名为卡片尺寸语义 ----
rep("UI_MONITOR_MIN_H = 260", "UI_CARD_LIVE = (720, 260)        # 硬件监测卡片（指定尺寸）")
rep("UI_BREAKDOWN_MIN_H = 570",
    "UI_CARD_BREAKDOWN = (500, 570)   # 功耗结构卡片（指定尺寸）")

# ---- 写回 ----
out = s.replace("\n", "\r\n") if CRLF else s
io.open(P, "w", encoding="utf-8", newline="").write(out)
print("PATCH OK: %d 处替换，CRLF=%s，行数 %d -> %d"
      % (n, CRLF, orig.count("\n") + 1, s.count("\n") + 1))
