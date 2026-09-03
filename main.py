# -*- coding: utf-8 -*-
"""PC 电脑用电电费计算器（离线桌面版）。

功能：
  1. 启动即拉取本机配置（CPU/GPU/内存/磁盘/显示器/系统）。
  2. 实时采样并计算瞬时功耗：GPU 用 nvidia-smi 真实功耗，CPU 按负载估算，
     其余部件用静态功耗模型；折算插座功耗（含电源损耗）。
  3. 累计电量(kWh)与电费(¥)，支持暂停/继续、会话持久化（重开可续算）。
  4. 默认 24 小时倒计时；到点自动生成汇总报告，可随时手动导出。

运行：python main.py   （需 PySide6）
"""
from __future__ import annotations

import sys
import os
import json
import time
import threading
from datetime import datetime, timedelta
from collections import deque

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QElapsedTimer, QDateTime, QEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QLineEdit, QDoubleSpinBox, QSpinBox,
    QDialog, QFormLayout, QMessageBox, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QSizePolicy, QSpacerItem, QCheckBox, QSystemTrayIcon, QMenu,
    QProgressBar, QInputDialog, QTextBrowser, QComboBox, QScrollArea,
)
from PySide6.QtCharts import (QChart, QChartView, QLineSeries, QValueAxis,
                              QBarSeries, QBarSet, QBarCategoryAxis, QDateTimeAxis)
from PySide6.QtGui import QPainter, QFont, QColor, QAction, QPixmap, QIcon

import hardware as H
import power_model as PM

DEFAULT_RATE = 0.56          # 元 / 千瓦时（居民电价参考，可在设置中修改）
APP_VERSION = "v18.13"       # 界面标题/托盘提示展示的版本号
WINDOW_HOURS = 24.0
SAMPLE_MS = 2000
# v18.13 常见电源额定功率档位：给「按推荐填入」取最接近的档，避免填出 543W 这种不存在的规格
PSU_COMMON = (300, 350, 400, 450, 500, 550, 600, 650, 700, 750, 800, 850, 1000)
# v18.11 手动标记「显示器已关」后，若检测到键鼠空闲短于该值，
# 说明人已回来，自动恢复按开屏计费，避免忘记切回导致长期低估
DISP_WAKE_IDLE_SEC = 20.0
# 会话文件位置：打包成 exe 后用可执行文件所在目录（__file__ 会指向临时解压目录）
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(BASE_DIR, "session.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
# v18.11 单实例心跳：仅凭互斥体不够——进程被强杀后可能残留僵尸仍持有互斥体
#（实测 PID 21300 杀不掉），导致后续所有实例都被误判「已在运行」而弹窗阻塞、
# 永远无法启动。故用心跳文件判定持有者是否真活着：心跳过期即视为已死，允许接管。
LOCK_FILE = os.path.join(BASE_DIR, "app.lock")
LOCK_FRESH_SEC = 45.0      # 心跳新鲜阈值（心跳每 20s 写一次）


def _beat():
    """写入心跳：时间戳 + 本进程 PID。

    v18.13 起带上 PID —— 只有时间戳无法区分「还活着」和「刚被关掉」：
    刚退出的实例心跳也是新鲜的，45 秒内重启会被自己拦在门外。
    """
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write("%.3f %d" % (time.time(), os.getpid()))
    except Exception:
        pass


def _last_beat() -> float:
    """读取上次心跳时间戳（无文件或损坏返回 0）。兼容旧版只有时间戳的格式。"""
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            return float((f.read().split() or ["0"])[0])
    except Exception:
        return 0.0


def _beat_pid() -> int:
    """心跳里记录的持有者 PID（旧格式无 PID 时返回 0）。"""
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            parts = f.read().split()
        return int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 0


def _pid_alive(pid: int) -> bool:
    """进程是否仍在运行（v18.13）。心跳新鲜不等于持有者还活着。"""
    if not pid:
        return False
    if sys.platform != "win32":
        try:
            os.kill(int(pid), 0)
            return True
        except Exception:
            return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259      # 259 = STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False

CSS = """
QMainWindow { background: #f4f6f9; }
QWidget#card { background: #ffffff; border-radius: 12px; border: 1px solid #e6e9ef; }
QLabel#title { font-size: 13px; color: #8a93a6; }
QLabel#big { font-size: 34px; font-weight: 700; color: #1f2a44; }
QLabel#sub { font-size: 12px; color: #8a93a6; }
QLabel#hw { font-size: 13px; color: #2b3552; }
QPushButton {
    background: #2f6bff; color: #fff; border: none; border-radius: 8px;
    padding: 9px 16px; font-size: 13px; font-weight: 600;
}
QPushButton:hover { background: #2559e0; }
QPushButton#ghost { background: #eef1f7; color: #2b3552; }
QPushButton#ghost:hover { background: #e2e7f1; }
QPushButton#danger { background: #ff5b6e; }
QPushButton#danger:hover { background: #e8485b; }
QLineEdit, QDoubleSpinBox, QSpinBox {
    border: 1px solid #d8dde8; border-radius: 7px; padding: 6px 9px;
    background: #fff; font-size: 13px;
}
QTableWidget { border: none; background: #fff; gridline-color: #eef1f7; }
QHeaderView::section { background: #f4f6f9; color: #6b7488; border: none;
    padding: 6px; font-size: 12px; }
"""


class SampleWorker(QThread):
    sample_ready = Signal(dict)

    def __init__(self, interval_ms: int = SAMPLE_MS, gpu_nvidia: bool = True):
        super().__init__()
        self.interval = interval_ms
        self.gpu_nvidia = gpu_nvidia
        self._timer = None
        self._sampler = None
        self._pending_interval = None

    def run(self):
        # 常驻采样器：psutil 进程内读 CPU 负载与各进程 CPU 时间（零子进程），
        # N 卡 nvidia-smi -l 常驻流 + 后台读线程缓存最新值；断流自动回退。
        try:
            self._sampler = H.PersistentSampler(self.interval, gpu_nvidia=self.gpu_nvidia)
            self._sampler.start()
        except Exception as _e:
            self._sampler = None
        self._timer = QTimer()
        # v18.4 关键修复：必须 DirectConnection，让 _tick 在 worker 线程内执行。
        # 旧默认 AutoConnection 按接收者(self，属主在主线程)排队 → 重活 sample()
        # (全进程 CPU 时间扫描，进程一多就是几十 ms) 一直在 UI 线程跑——界面卡顿的总根源。
        self._timer.timeout.connect(self._tick, Qt.ConnectionType.DirectConnection)
        self._timer.start(self.interval)
        self.exec()

    def _tick(self):
        # v18.4：先应用待生效的采样周期（必须在 worker 线程内做——
        # QTimer/采样器只允许在属主线程操作，否则 _tick 会被拖到主线程执行造成界面卡顿）
        pi = self._pending_interval
        if pi is not None and pi != self.interval:
            self._pending_interval = None
            self.interval = pi
            if self._timer:
                self._timer.setInterval(pi)
            try:
                if self._sampler:
                    self._sampler.stop()
            except Exception:
                pass
            try:
                self._sampler = H.PersistentSampler(pi, gpu_nvidia=self.gpu_nvidia)
                self._sampler.start()
            except Exception:
                self._sampler = None
        try:
            data = self._sampler.sample() if self._sampler else H.sample_load()
        except Exception:
            data = {"cpu_load": 0.0, "gpu_power": None, "gpu_valid": False}
        self.sample_ready.emit(data)

    def set_interval(self, ms: int):
        # v18.4 线程安全：UI 线程只写普通 int 属性，真正的重启由 worker 线程在下一 tick 应用。
        # 旧实现在 UI 线程跨线程 setInterval + 重启采样器 → 设置框保存后主界面周期性卡顿。
        self._pending_interval = int(ms)

    def stop(self):
        try:
            if self._sampler:
                self._sampler.stop()
        except Exception:
            pass
        self._sampler = None


class MiniOverlay(QWidget):
    """v18.8 桌面迷你悬浮窗：置顶、无边框半透明、可拖动；双击隐藏。
    独立顶层窗口（不挂父级），主窗口隐藏入托盘时不受影响。"""
    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedSize(196, 104)
        root = QVBoxLayout(self); root.setContentsMargins(14, 10, 14, 10); root.setSpacing(1)
        self.lbl_w = QLabel("— W")
        self.lbl_w.setStyleSheet("color:#eef3ff; font-size:26px; font-weight:800;")
        self.lbl_sub = QLabel("监测中")
        self.lbl_sub.setStyleSheet("color:#9fb4d8; font-size:11px;")
        self.lbl_cost = QLabel("")
        self.lbl_cost.setStyleSheet("color:#ffd28a; font-size:12px; font-weight:600;")
        root.addWidget(self.lbl_w); root.addWidget(self.lbl_sub); root.addWidget(self.lbl_cost)
        self._drag = None

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.NoPen); p.setBrush(QColor(18, 26, 44, 196))
        p.drawRoundedRect(self.rect(), 12, 12)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def mouseDoubleClickEvent(self, e):
        self.hide()   # 双击隐藏（托盘菜单可再开）


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"PC 电脑用电电费计算器 {APP_VERSION}")
        self.resize(1080, 760)
        self.setStyleSheet(CSS)

        # ---- 状态 ----
        self.hw = H.detect_hardware()
        self.model = PM.build_model(self.hw)
        self.calib_k = 1.0
        self.calib_idle = 0.0
        self.calib_peak = 0.0
        self.model.calib_k = self.calib_k
        self.model.calib_idle = self.calib_idle
        self.model.calib_peak = self.calib_peak
        self.rate = DEFAULT_RATE
        self.window_hours = WINDOW_HOURS
        self.psu_eff = PM.PSU_EFFICIENCY
        # v18.11 电源额定功率 W：>0 时按 80 PLUS 曲线动态算效率，
        # 0 表示不启用（沿用上面的固定效率，行为与旧版一致）
        self.psu_rating_w = 0.0
        self.sample_ms = SAMPLE_MS
        # v18.8 迷你悬浮窗状态
        self._mini_visible = False
        self._mini_pos = None
        # v18.9 每日日报：跨天检测锚点（首拍落今日，跨零点自动出前一日日报）
        self._last_date = None
        # 计费方式：单一 / 峰谷 / 阶梯
        self.price_mode = "单一"
        self.rate_valley = 0.30      # 谷价 元/度
        self.rate_flat = 0.56        # 平价
        self.rate_peak = 0.85        # 峰价
        self.tou_valley = (23, 7)    # (起, 止) 小时，可跨午夜
        self.tou_peak = [(8, 11), (18, 21)]
        # 阶梯电价（居民月用量分档）
        self.tier_base = 0.0         # 本月已用基数 kWh（监测开始前）
        self.tier_l1 = 2160.0        # 第一档上限 kWh
        self.tier_r1 = 0.56
        self.tier_l2 = 4800.0        # 第二档上限 kWh
        self.tier_r2 = 0.61
        self.tier_r3 = 0.86          # 第三档（超出 l2）

        self.energy_wh = 0.0          # 累计电量 Wh
        self.peak_wall = 0.0
        self.running_elapsed_ms = 0.0 # 已运行（监测中）时长
        self.running = False
        self.finished = False
        self.last_ts = None
        # (epoch_s, wall_w, sys_w, cpu_load, gpu_power)
        self.all_samples = deque(maxlen=60000)
        self.hourly = {}                         # hour_key -> [sum_w, n]
        self.period_energy_wh = {"谷": 0.0, "平": 0.0, "峰": 0.0}
        self.cur = {"cpu_load": 0.0, "gpu_power": None, "gpu_valid": False,
                    "wall": 0.0, "sys": 0.0, "breakdown": {}}
        # 性能：样式档位缓存（颜色没变就不重复 setStyleSheet，避免每帧重绘）
        self._budget_col = None
        self._idle_col = None
        # 软件耗电：按进程 CPU 时间增量分摊 CPU 估算功耗（运行时累计，不持久化）
        self.app_cpu_snap = (0.0, {})   # (ts, {进程名: 累计CPU秒})
        self.app_cpu_wh = {}            # 进程名 -> CPU 能耗 Wh

        # 托盘 / 开机自启
        self.tray = None
        self._force_quit = False
        self.autostart = self._read_autostart()
        self.autostart_monitor = False
        # 功耗告警
        self.alert_enabled = False
        self.alert_threshold = 300.0   # 插座功耗阈值 W
        self._alert_active = False
        # 月度用电预算
        self.budget_kwh = 0.0          # 月度电量预算 kWh（0=不设）
        self.budget_cost = 0.0         # 月度电费预算 ¥（0=不设）
        self.budget_alert_enabled = False
        self.budget_alert_pct = 90.0   # 达到预算该比例时预警
        self._budget_alert_active = False
        # 待机功耗识别与统计
        self.idle_cpu_thresh = 5.0     # CPU 负载低于此值视为空闲(%)
        self.idle_gpu_thresh = 15.0    # GPU 真实功耗低于此值(或核显)视为空闲(W)
        self.idle_energy_wh = 0.0      # 空闲时段累计电量 Wh
        self.active_energy_wh = 0.0    # 活跃时段累计电量 Wh
        self.idle_streak_ms = 0.0      # 当前连续空闲时长 ms
        self.idle_streak_energy_wh = 0.0  # 本次连续空闲已耗电量 Wh
        self.idle_nudge_enabled = False
        self.idle_nudge_min = 10.0     # 连续空闲达该分钟数弹提醒
        self._idle_nudged = False

        # v18.11 显示器开关状态：None=自动推断，True/False=用户手动覆盖
        self._disp_manual = None
        self.display_on = True
        self._disp_saved_wh = 0.0      # 熄屏期间累计省下的电量 Wh

        self._build_ui()
        # v18 系统信息侧栏静态数据（一次性 WMI/注册表采集）
        try:
            self._sys_static = H.collect_system_info()
        except Exception as _e:
            self._sys_static = {}
        self._load_session_maybe()
        # v18 常驻监测：启动即开始，无需手动操作
        if not self.finished:
            self.running = True
            self.last_ts = time.time()
            self.status_lbl.setText("监测中…（后台常驻）")
        # v18 开机自启默认开启（用户可在设置里关闭）
        if not self.autostart:
            try:
                self.autostart = True
                self._set_autostart(True)
            except Exception:
                pass
        # v18 不占任务栏：作为工具窗口（托盘双击唤出）
        try:
            self.setWindowFlag(Qt.WindowType.Tool, True)
        except Exception:
            pass
        self._setup_tray()
        # v18.8 迷你悬浮窗：按会话恢复显示与位置
        self.mini = MiniOverlay()
        self.mini.hide()
        if self._mini_visible:
            if self._mini_pos:
                self.mini.move(int(self._mini_pos[0]), int(self._mini_pos[1]))
            self.mini.show()
            if self.tray is not None and getattr(self, "a_mini", None) is not None:
                self.a_mini.setChecked(True)
        self._start_worker()
        # v18.11 心跳：证明本实例存活，避免被后续实例误判为僵尸
        self._beat_timer = QTimer(self)
        self._beat_timer.timeout.connect(_beat)
        self._beat_timer.start(20000)
        _beat()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # v18：AIDA64 风格系统信息面板固定在最左侧
        outer.addWidget(self._sysinfo_panel())
        page = QVBoxLayout()
        page.setContentsMargins(18, 16, 18, 16)
        page.setSpacing(14)
        outer.addLayout(page, 1)
        # v18.9：设置面板改为抽屉式浮层——不挤占布局宽度，打开时浮在内容上方右侧
        self._settings_dock = QScrollArea(root)
        self._settings_dock.setWidgetResizable(True)
        self._settings_dock.setFrameShape(QFrame.Shape.NoFrame)
        self._settings_dock.setFixedWidth(400)
        self._settings_dock.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # v18.11 半透明抽屉：背景 92% 白 + viewport 同步透明，否则子控件会盖成实心
        self._settings_dock.setStyleSheet(
            "QScrollArea { background: rgba(255,255,255,216); border: none; "
            "border-left: 2px solid rgba(47,107,255,190); }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QScrollArea QScrollBar:vertical { background: rgba(0,0,0,18); width: 8px; }")
        self._settings_dock.setWidget(self._build_settings_panel())
        self._settings_dock.hide()

        # 顶部标题栏
        top = QHBoxLayout()
        t = QLabel("⚡ PC 用电电费计算器")
        t.setFont(QFont("Microsoft YaHei", 18, QFont.Weight.Bold))
        t.setStyleSheet("color:#1f2a44;")
        sub = QLabel(f"实时监测 · 24 小时汇总 · 完全离线 · {APP_VERSION}")
        sub.setStyleSheet("color:#8a93a6;font-size:12px;")
        top.addWidget(t)
        top.addItem(QSpacerItem(20, 10, QSizePolicy.Expanding))
        top.addWidget(sub)
        page.addLayout(top)

        # v18.5：本机配置卡片已删除——硬件信息由最左侧系统信息面板全量承载，避免重复

        # 三大实时读数 + 倒计时
        page.addWidget(self._live_card())

        # 图表 + 明细
        mid = QHBoxLayout()
        mid.setSpacing(14)
        mid.addWidget(self._chart_card(), 3)
        mid.addWidget(self._breakdown_card(), 2)
        page.addLayout(mid, 1)

        # 控制条
        page.addWidget(self._control_bar())

    def _card(self, widget: QWidget):
        widget.setObjectName("card")
        return widget

    def _hw_card(self) -> QWidget:
        c = QWidget(); self._card(c)
        lay = QHBoxLayout(c); lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(22)
        ram = f"{self.hw.ram_bytes/1e9:.1f} GB" if self.hw.ram_bytes else "—"
        disk = ", ".join(f"{t} {s:.0f}G" for t, s in self.hw.disks) or "—"
        items = [
            ("处理器", self.hw.cpu_name or "—",
             f"{self.hw.cpu_cores}C / {self.hw.cpu_threads}T" if self.hw.cpu_cores else ""),
            ("显卡", self.hw.gpu_name or "—",
             (f"{self.hw.gpu_vram_bytes/1e9:.1f} GB · {self.hw.gpu_resolution}"
              if self.hw.gpu_vram_bytes else "核显/未知")),
            ("内存", ram, ""),
            ("存储", disk, f"{len(self.hw.disks)} 块"),
            ("显示器", f"{self.hw.monitor_count} 台", self.hw.gpu_resolution or ""),
            ("系统", self.hw.os_caption or "Windows", "台式机" if not self.hw.has_battery else "笔记本"),
        ]
        for label, val, small in items:
            col = QVBoxLayout(); col.setSpacing(3)
            l1 = QLabel(label); l1.setObjectName("title")
            l2 = QLabel(val); l2.setObjectName("hw"); l2.setWordWrap(True)
            l2.setMaximumWidth(190)
            l3 = QLabel(small); l3.setObjectName("sub")
            col.addWidget(l1); col.addWidget(l2); col.addWidget(l3)
            lay.addLayout(col)
        lay.addItem(QSpacerItem(10, 10, QSizePolicy.Expanding))
        return c

    def _live_card(self) -> QWidget:
        c = QWidget(); self._card(c)
        lay = QHBoxLayout(c); lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(18)

        # 插座功耗（大）
        col1 = QVBoxLayout(); col1.setSpacing(2)
        col1.addWidget(self._lbl("插座实时功耗", "title"))
        self.wall_big = self._lbl("0.0 W", "big")
        col1.addWidget(self.wall_big)
        self.wall_sub = self._lbl("系统功耗 0.0 W（含电源损耗）", "sub")
        col1.addWidget(self.wall_sub)
        lay.addLayout(col1, 2)

        # 累计电量
        col2 = QVBoxLayout(); col2.setSpacing(2)
        col2.addWidget(self._lbl("累计电量", "title"))
        self.energy_big = self._lbl("0.000 kWh", "big")
        col2.addWidget(self.energy_big)
        self.cost_sub = self._lbl("电费 ¥0.00", "sub")
        col2.addWidget(self.cost_sub)
        lay.addLayout(col2, 2)

        # 倒计时
        col3 = QVBoxLayout(); col3.setSpacing(2)
        col3.addWidget(self._lbl("距 24h 汇总", "title"))
        self.cd_big = self._lbl("24:00:00", "big")
        col3.addWidget(self.cd_big)
        self.rate_sub = self._lbl(f"电价 {self.rate:.2f} 元/度 ▸ 点击改价", "sub")
        self.rate_sub.setCursor(Qt.CursorShape.PointingHandCursor)
        self.rate_sub.setToolTip("点击直接修改每度电价格（峰谷/阶梯计费在「设置」里调整）")
        self.rate_sub.installEventFilter(self)
        col3.addWidget(self.rate_sub)
        lay.addLayout(col3, 2)

        # CPU/GPU 负载
        col4 = QVBoxLayout(); col4.setSpacing(2)
        col4.addWidget(self._lbl("实时负载", "title"))
        self.cpu_lbl = self._lbl("CPU 0%", "hw"); self.cpu_lbl.setStyleSheet("font-size:16px;font-weight:600;color:#2b3552;")
        self.gpu_lbl = self._lbl("GPU —", "hw"); self.gpu_lbl.setStyleSheet("font-size:16px;font-weight:600;color:#2b3552;")
        col4.addWidget(self.cpu_lbl); col4.addWidget(self.gpu_lbl)
        self.psu_lbl = self._lbl("", "sub")
        col4.addWidget(self.psu_lbl)
        lay.addLayout(col4, 2)

        # 预估电费（每小时 / 每24小时 / 每月）
        col5 = QVBoxLayout(); col5.setSpacing(3)
        col5.addWidget(self._lbl("预估电费", "title"))
        self.proj_hour = self._lbl("—", "big"); self.proj_hour.setStyleSheet("font-size:17px;font-weight:600;color:#2b3552;")
        self.proj_day = self._lbl("—", "big"); self.proj_day.setStyleSheet("font-size:17px;font-weight:600;color:#2b3552;")
        self.proj_month = self._lbl("—", "big"); self.proj_month.setStyleSheet("font-size:17px;font-weight:600;color:#c2410c;")
        self._proj_rows = []  # PySide6: 必须持有子布局引用，否则循环重绑变量名会被 GC 删掉 C++ 对象
        for cap, w in (("每小时", self.proj_hour), ("每24小时", self.proj_day), ("每月30天", self.proj_month)):
            row = QHBoxLayout(); row.setSpacing(6)
            cap_lbl = self._lbl(cap, "sub"); cap_lbl.setMinimumWidth(60)
            row.addWidget(cap_lbl); row.addWidget(w, 1)
            col5.addLayout(row); self._proj_rows.append(row)
        lay.addLayout(col5, 2)

        # 月度预算进度
        col6 = QVBoxLayout(); col6.setSpacing(2)
        col6.addWidget(self._lbl("月度预算", "title"))
        self.budget_big = self._lbl("未设", "big")
        col6.addWidget(self.budget_big)
        self.budget_bar = QProgressBar()
        self.budget_bar.setRange(0, 100); self.budget_bar.setValue(0)
        self.budget_bar.setTextVisible(False)
        self.budget_bar.setFixedHeight(9)
        self.budget_bar.setStyleSheet(
            "QProgressBar{border:none;border-radius:5px;background:#eef1f7;}"
            "QProgressBar::chunk{background:#2fae6b;border-radius:5px;}")
        col6.addWidget(self.budget_bar)
        self.budget_sub = self._lbl("", "sub")
        col6.addWidget(self.budget_sub)
        lay.addLayout(col6, 2)

        # 待机占比
        col7 = QVBoxLayout(); col7.setSpacing(2)
        col7.addWidget(self._lbl("待机占比", "title"))
        self.idle_big = self._lbl("—", "big")
        col7.addWidget(self.idle_big)
        self.idle_sub = self._lbl("空闲时段耗电", "sub")
        col7.addWidget(self.idle_sub)
        lay.addLayout(col7, 2)

        return c

    def _chart_card(self) -> QWidget:
        c = QWidget(); self._card(c)
        lay = QVBoxLayout(c); lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        h = QLabel("功耗曲线（近 60 分钟 · 插座功耗 W）")
        h.setStyleSheet("font-size:13px;color:#2b3552;font-weight:600;")
        lay.addWidget(h)
        self.series = QLineSeries()
        self.series.setColor(QColor("#2f6bff"))
        self.chart = QChart()
        self.chart.addSeries(self.series)
        self.chart.legend().hide()
        self.chart.setBackgroundVisible(False)
        # x 轴用绝对时刻（QDateTimeAxis）：点一旦加入坐标不变，可增量追加/淘汰，
        # 避免旧实现（x=分钟前，每帧坐标漂移）被迫每 2s 全量 clear+重建导致的卡顿。
        from PySide6.QtCore import QDateTime
        self.axis_x = QDateTimeAxis(); self.axis_y = QValueAxis()
        self.axis_x.setFormat("HH:mm"); self.axis_y.setTitleText("W")
        self.axis_x.setRange(QDateTime.fromMSecsSinceEpoch(int(time.time() * 1000) - 3_600_000),
                             QDateTime.fromMSecsSinceEpoch(int(time.time() * 1000)))
        self.axis_y.setRange(0, 100)
        self.chart.addAxis(self.axis_x, Qt.AlignmentFlag.AlignBottom)
        self.chart.addAxis(self.axis_y, Qt.AlignmentFlag.AlignLeft)
        self.series.attachAxis(self.axis_x); self.series.attachAxis(self.axis_y)
        self._chart_pts = deque()          # 与 series 同步的 (ts_ms, W) 镜像
        self._chart_xbucket = -1           # x 轴范围 15s 快照桶，避免每帧重排版
        self._chart_max_bucket = -1        # y 轴量程变化去抖
        self.chart_view = QChartView(self.chart)
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        lay.addWidget(self.chart_view, 1)
        return c

    def _breakdown_card(self) -> QWidget:
        c = QWidget(); self._card(c)
        lay = QVBoxLayout(c); lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        h = QLabel("功耗构成（估算）")
        h.setStyleSheet("font-size:13px;color:#2b3552;font-weight:600;")
        lay.addWidget(h)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["部件", "功耗 W"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        lay.addWidget(self.table, 1)
        # PSU 建议
        self.psu_hint = QLabel("")
        self.psu_hint.setStyleSheet("font-size:12px;color:#6b7488;")
        lay.addWidget(self.psu_hint)
        return c

    # ---------------- 系统信息侧栏（AIDA64 风格，最左侧） ----------------
    def _sysinfo_panel(self) -> QWidget:
        c = QWidget()
        c.setFixedWidth(375)
        c.setStyleSheet("background:#ffffff;border-right:1px solid #e6e9ef;")
        v = QVBoxLayout(c); v.setContentsMargins(8, 8, 8, 8); v.setSpacing(0)
        self.sysinfo_view = QTextBrowser()
        self.sysinfo_view.setStyleSheet(
            "QTextBrowser{background:#ffffff;color:#1a1a1a;border:none;"
            "font-family:'Consolas','Microsoft YaHei';font-size:12px;}")
        self.sysinfo_view.setOpenExternalLinks(False)
        self.sysinfo_view.setFrameShape(QFrame.Shape.NoFrame)
        # v18.12 硬件重检测：插拔硬盘/显示器、换硬件后手动重建功耗模型，
        # 否则模型只在启动时构建一次，之后增减硬件估算值不会变。
        hdr = QWidget(); hh = QHBoxLayout(hdr)
        hh.setContentsMargins(0, 0, 0, 6); hh.setSpacing(6)
        btn_rd = QPushButton("⟳ 重新检测硬件")
        btn_rd.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_rd.setToolTip("插拔硬盘 / 显示器、更换硬件后点此重建功耗模型。\n"
                          "静态功耗与部件清单会随之变化，电源负载率与转换效率也跟着重算。\n"
                          "（校准数据与电源额定功率设置会保留）")
        btn_rd.setStyleSheet(
            "QPushButton{background:#f4f6fa;border:1px solid #d6dbe6;border-radius:5px;"
            "padding:5px 10px;font-size:12px;color:#2f3b52;}"
            "QPushButton:hover{background:#e8eefb;border-color:#9db4e8;}")
        btn_rd.clicked.connect(self._redetect_hardware)
        hh.addWidget(btn_rd)
        self._hw_detect_lbl = QLabel("启动时已检测")
        self._hw_detect_lbl.setStyleSheet("font-size:11px;color:#8a94a6;")
        hh.addWidget(self._hw_detect_lbl, 1)
        v.addWidget(hdr)
        v.addWidget(self.sysinfo_view, 1)
        self._sys_dyn = {}
        self._sys_static = {}
        return c

    @staticmethod
    def _bar(pct: float, width: int = 12) -> str:
        pct = max(0.0, min(100.0, pct))
        n = int(pct / 100.0 * width + 0.5)
        col = "#5aa832" if pct < 70 else ("#d99a17" if pct < 90 else "#d8492f")
        return (f"<span style='color:{col}'>{'█' * n}</span>"
                f"<span style='color:#d8d8d8'>{'█' * (width - n)}</span> {pct:.1f}%")

    @staticmethod
    def _temp_html(t, warn: float, hot: float) -> str:
        if t is None:
            return "<span style='color:#999999'>—</span>"
        col = "#1a1a1a" if t < warn else ("#d99a17" if t < hot else "#d8492f")
        return f"<span style='color:{col}'>{t:.0f}°</span>"

    def _build_sysinfo_html(self) -> str:
        s = self._sys_static or {}
        dyn = self._sys_dyn or {}
        g = s.get
        import datetime as _dt
        boot = dyn.get("boot")
        if boot:
            up = max(0, time.time() - boot)
            hh, rem = divmod(int(up), 3600); mm, ss = divmod(rem, 60)
            uptime = f"{hh:02d}时{mm:02d}分{ss:02d}秒"
            now = datetime.now()
            wk = "一二三四五六日"[now.weekday()]
            upt_line = f"{uptime}　{now:%Y-%m-%d} [{wk}] {now:%H:%M:%S}"
        else:
            upt_line = "—"
        # 内存
        ram_total = dyn.get("ram_total") or self.hw.ram_bytes or 0
        ram_used = dyn.get("ram_used") or 0
        ram_free = ram_total - ram_used
        ram_pct = dyn.get("ram_pct") or 0.0
        def gb(b):
            return f"{b / (1 << 30):.2f}GB" if b else "—"
        cpu_t = dyn.get("cpu_temp")
        gpu_t = dyn.get("gpu_temp")
        mhz = dyn.get("mhz") or (s.get("mhz") or 0)
        dpi = 96
        try:
            from PySide6.QtWidgets import QApplication as _QA
            scr = _QA.primaryScreen()
            if scr:
                dpi = int(scr.logicalDotsPerInch())
        except Exception:
            pass
        vram = s.get("gpuvram")
        vram_txt = f"{vram / (1 << 30):.0f}GB" if vram else "—"
        fw = s.get("fw") or ""
        fw_txt = "UEFI" if fw == "UEFI" else ("BIOS" if "Legacy" in fw else (fw or "—"))
        sb_txt = "启用" if s.get("sb") == 1 else ("关闭" if fw_txt == "UEFI" else "—")
        Y = "<span style='color:#111111;font-weight:bold'>"   # v18.5 白底黑字：标签黑色加粗
        E = "</span>"
        L = []
        L.append(f"<div style='margin:2px 0 6px 0;'>{Y}运行时间{E} {upt_line}</div>")
        L.append(f"<div style='margin-bottom:6px;'>{Y}操作系统{E} {g('os') or '—'} "
                 f"{g('osarch') or ''} 10.0.{g('osver') or ''}</div>")
        L.append(f"<div style='margin-bottom:6px;'>{Y}启动模式{E} {fw_txt}　"
                 f"安全引导: {sb_txt}　TPM模块: {g('tpm') or '—'}</div>")
        L.append(f"<div style='margin-bottom:6px;'>{Y}计算机名{E} {g('comp') or '—'} ({g('domain') or '—'})</div>")
        L.append(f"<div style='margin-bottom:6px;'>{Y}主板型号{E} {g('mb') or '—'}</div>")
        L.append(f"<div style='margin-bottom:6px;'>{Y}主板Bios{E} Ver: {g('bios') or '—'}</div>")
        cpu_name = (g('cpu') or self.hw.cpu_name or "—").strip()
        L.append(f"<div style='margin-bottom:2px;'>{Y}处 理 器{E} {cpu_name}</div>")
        L.append(f"<div style='margin:0 0 2px 46px;'>核心: {g('cores') or '—'} × 线程: "
                 f"{g('threads') or '—'}　频率: {mhz / 1000.0:.2f} GHz　{self._temp_html(cpu_t, 75, 85)}</div>")
        def kb2mb(v):
            try:
                kb = int(v)
                return f"{kb / 1024.0:.0f}MB" if kb >= 1024 else f"{kb}KB"
            except Exception:
                return "—"
        L.append(f"<div style='margin:0 0 2px 46px;'>Cache缓存 L1={kb2mb(g('l1'))}　"
                 f"L2={kb2mb(g('l2'))}　L3={kb2mb(g('l3'))}</div>")
        cpu_load = self.cur.get("cpu_load") if isinstance(self.cur, dict) else None
        if cpu_load is None:
            cpu_load = 0.0
        L.append(f"<div style='margin:0 0 8px 46px;'>利用率: {self._bar(cpu_load)}</div>")
        L.append(f"<div style='margin-bottom:2px;'>{Y}物理内存{E} 总内存: {gb(ram_total)}　"
                 f"插槽数: {g('slots') or '—'}　最大支持: "
                 f"{int((g('maxcap') or 0) / (1 << 20))}GB</div>")
        L.append(f"<div style='margin:0 0 2px 46px;'>已使用: {gb(ram_used)}　"
                 f"可使用: {gb(ram_free)}</div>")
        L.append(f"<div style='margin:0 0 2px 46px;'>使用率: {self._bar(ram_pct)}</div>")
        for m in (s.get("mods") or [])[:4]:
            mn = (m.get("m") or "").replace("Unknown", "GeIL")
            cap = int(m.get("cap") or 0) / (1 << 30)
            L.append(f"<div style='margin:0 0 1px 46px;'>{mn} {m.get('pn') or ''} "
                     f"DDR4/{m.get('clk') or m.get('spd') or '—'} {cap:.0f}GB</div>")
        L.append(f"<div style='margin:2px 0 2px 0;'>{Y}图形显示{E} {g('gpures') or '—'}\"　"
                 f"{g('gpuref') or '—'}Hz　DPI: {dpi}</div>")
        L.append(f"<div style='margin:0 0 8px 46px;'>{g('gpuname') or self.hw.gpu_name}　"
                 f"{vram_txt}　{self._temp_html(gpu_t, 65, 78)}</div>")
        dtemps = dyn.get("disk_temps") or {}
        for i, dk in enumerate(g('disks') or []):
            media = (dk.get("media") or "").upper()
            tag = "SSD" if "SSD" in media else ("HDD" if "HDD" in media else (dk.get("bus") or ""))
            dt = dtemps.get(dk.get("model") or "")
            L.append(f"<div style='margin-bottom:1px;'>{Y}磁盘信息{E} {i}: {dk.get('model') or '—'} "
                     f"[{tag}]　{dk.get('letters') or ''}　{dk.get('sizeGB') or '—'}GB　"
                     f"{self._temp_html(dt, 45, 55)}</div>")
        nic = g('nic')
        if nic:
            L.append(f"<div style='margin:2px 0 1px;'>{Y}网络连接{E} {nic}</div>")
            L.append(f"<div style='margin:0 0 1px 46px;'>MAC: {g('mac') or '—'}　速率: 1Gbps</div>")
            L.append(f"<div style='margin:0 0 1px 46px;'>IP: {g('ip') or '—'} 以太网</div>")
            L.append(f"<div style='margin:0 0 6px 46px;'>网关: {g('gw') or '—'}</div>")
        dn = dyn.get("down_kbs"); upk = dyn.get("up_kbs")
        L.append(f"<div style='margin-top:2px;'>{Y}网络速率{E} ↓: "
                 f"<span style='color:#5aa832'>{dn:.1f} K/s</span>　↑: "
                 f"<span style='color:#5aa832'>{upk:.1f} K/s</span></div>" if dn is not None and upk is not None
                 else f"<div style='margin-top:2px;'>{Y}网络速率{E} ↓: —　↑: —</div>")
        return "".join(L)

    def _update_sysinfo(self):
        # v18.2 节流：QTextBrowser.setHtml 是整篇富文本重解析+重排版，每 2s 一次会拖累 UI；
        # 改为约 6s 刷新一次，并保持滚动位置不被重置。
        self._sys_tick = getattr(self, "_sys_tick", 0) + 1
        if self._sys_tick % 3 != 1:
            return
        try:
            sb = self.sysinfo_view.verticalScrollBar()
            pos = sb.value()
            self.sysinfo_view.setHtml(self._build_sysinfo_html())
            sb.setValue(pos)
        except Exception:
            pass

    def _suggest_psu_rating(self) -> int:
        """v18.13 估算电源额定功率：主机峰值 ÷ 标称效率 + 30% 余量，取最接近的常见档位。

        多数用户说不清电源铭牌，而「额定功率 = 0」意味着动态效率曲线一直不启用。
        给一个基于当前硬件的估算起点，比停在 0 上强；估算值只用于取档位，
        不参与任何功耗计算，用户随时可改成真实铭牌值。
        """
        try:
            _mon_pk = float(getattr(self.model, "monitor_w", 0.0) or 0.0)
            host_pk = max(1.0, float(self.model.peak_sys) - _mon_pk)
            need = host_pk / 0.88 * 1.3          # 0.88 = 中等负载标称效率
            return int(min(PSU_COMMON, key=lambda c: abs(c - need)))
        except Exception:
            return 0

    def _redetect_hardware(self):
        """v18.12 重新采集硬件并重建功耗模型。

        模型（CPU/GPU TDP、SSD/HDD 数量、显示器数量与分辨率、静态功耗）
        只在启动时构建一次，运行期间插拔硬件不会反映到估算里。
        这里重跑一遍检测，并原样继承与硬件无关的用户设置：
        电源效率 / 额定功率 / 校准系数，避免重检测把校准冲掉。
        """
        old = self.model
        try:
            hw = H.detect_hardware()
            m = PM.build_model(hw)
            try:
                static = H.collect_system_info()
            except Exception:
                static = None
        except Exception as e:
            QMessageBox.warning(self, "检测失败", f"重新检测硬件时出错：\n{e}")
            return

        # 继承非硬件相关的用户设置与校准
        m.psu_efficiency = getattr(old, "psu_efficiency", self.psu_eff)
        m.psu_rating_w = float(getattr(self, "psu_rating_w", 0.0) or 0.0)
        m.calib_k = getattr(old, "calib_k", 1.0)
        m.calib_idle = getattr(old, "calib_idle", 0.0)
        m.calib_peak = getattr(old, "calib_peak", 0.0)

        # 差异对比（只列发生变化的部件 + 静态功耗总计）
        oc = dict(getattr(old, "components", {}) or {})
        nc = dict(m.components)
        lines = []
        for k in sorted(set(oc) | set(nc)):
            a = float(oc.get(k, 0.0) or 0.0)
            b = float(nc.get(k, 0.0) or 0.0)
            if abs(a - b) > 0.05:
                lines.append(f"  · {k}：{a:.1f} W → {b:.1f} W")
        d_static = m.static_idle - float(getattr(old, "static_idle", 0.0) or 0.0)
        d_peak = m.peak_sys - float(getattr(old, "peak_sys", 0.0) or 0.0)

        self.hw = hw
        self.model = m
        self.model.psu_efficiency = self.psu_eff
        if static:
            self._sys_static = static
        self._sys_tick = 0            # 强制下一次刷新立即重绘
        self._update_sysinfo()

        stamp = datetime.now().strftime("%H:%M:%S")
        self._hw_detect_lbl.setText(f"{stamp} 已检测")
        if lines or abs(d_static) > 0.05:
            body = "\n".join(lines) if lines else "  · 部件未变，仅整体功耗微调"
            QMessageBox.information(
                self, "硬件已重新检测",
                f"{body}\n\n"
                f"静态功耗：{old.static_idle:.1f} W → {m.static_idle:.1f} W"
                f"（{d_static:+.1f} W）\n"
                f"峰值功耗：{old.peak_sys:.1f} W → {m.peak_sys:.1f} W"
                f"（{d_peak:+.1f} W）\n\n"
                f"校准与电源设置已保留。")
        else:
            QMessageBox.information(self, "硬件已重新检测",
                                    "未检测到硬件变化，功耗模型保持不变。")

    def _control_bar(self) -> QWidget:
        c = QWidget(); self._card(c)
        lay = QHBoxLayout(c); lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        # v18：始终监测，移除「开始监测」按钮
        self.btn_reset = QPushButton("重置"); self.btn_reset.setObjectName("ghost")
        self.btn_reset.clicked.connect(self.reset_session)
        self.btn_export = QPushButton("导出报告"); self.btn_export.setObjectName("ghost")
        self.btn_export.clicked.connect(self.export_report)
        self.btn_csv = QPushButton("导出CSV"); self.btn_csv.setObjectName("ghost")
        self.btn_csv.clicked.connect(self.export_csv)
        self.btn_compare = QPushButton("方案对比"); self.btn_compare.setObjectName("ghost")
        self.btn_compare.clicked.connect(self.open_compare)
        self.btn_history = QPushButton("历史趋势"); self.btn_history.setObjectName("ghost")
        self.btn_history.clicked.connect(self.open_history)
        self.btn_sim = QPushButton("节能模拟"); self.btn_sim.setObjectName("ghost")
        self.btn_sim.clicked.connect(self.open_sim)
        self.btn_hourly = QPushButton("时段分布"); self.btn_hourly.setObjectName("ghost")
        self.btn_hourly.clicked.connect(self.open_hourly)
        self.btn_settings = QPushButton("设置"); self.btn_settings.setObjectName("ghost")
        self.btn_settings.clicked.connect(self.open_settings)
        self.status_lbl = QLabel("就绪"); self.status_lbl.setStyleSheet("color:#6b7488;font-size:12px;")
        lay.addWidget(self.btn_reset)
        lay.addWidget(self.btn_export); lay.addWidget(self.btn_csv)
        self.btn_apps = QPushButton("软件耗电"); self.btn_apps.setObjectName("ghost")
        self.btn_apps.clicked.connect(self.open_apps)
        lay.addWidget(self.btn_compare); lay.addWidget(self.btn_history); lay.addWidget(self.btn_sim); lay.addWidget(self.btn_hourly); lay.addWidget(self.btn_apps); lay.addWidget(self.btn_settings)
        lay.addItem(QSpacerItem(20, 10, QSizePolicy.Expanding))
        lay.addWidget(self.status_lbl)
        return c

    @staticmethod
    def _lbl(text, kind):
        l = QLabel(text); l.setObjectName(kind); return l

    # ---------------- 采样 / 计算 ----------------
    def _start_worker(self):
        self.worker = SampleWorker(self.sample_ms, gpu_nvidia=self.hw.gpu_is_nvidia)
        self.worker.sample_ready.connect(self.on_sample)
        self.worker.start()

    # ---------------- v18.11 显示器开关状态 ----------------
    def _display_on(self) -> bool:
        """当前显示器是否点亮。手动覆盖优先，否则按系统空闲时长推断。

        注意：用户手动按显示器电源键关屏，系统层面无法感知，
        只能通过手动开关标记；系统自动熄屏可自动识别。
        """
        manual = getattr(self, "_disp_manual", None)
        if manual is True:
            return True
        if manual is False:
            # 手动标记熄屏：人回来动键鼠后自动恢复按开屏计，
            # 无需手动切回（避免忘记切回造成长期低估）
            try:
                if H.user_idle_sec() < DISP_WAKE_IDLE_SEC:
                    return True
            except Exception:
                pass
            return False
        try:
            return not H.display_auto_off()
        except Exception:
            return True

    def _set_display_manual(self, state):
        """state: None=自动检测 / True=强制记为开 / False=强制记为关（熄屏省电）"""
        self._disp_manual = state
        self.display_on = self._display_on()
        mon_w = float(getattr(self.model, "components", {}).get("显示器", 0.0) or 0.0)
        if state is False:
            self._notify("已按显示器关闭计算",
                         f"显示器功耗 {mon_w:.0f} W 已从估算中扣除，"
                         f"当前功率下调约 {mon_w:.0f} W。")
        elif state is True:
            self._notify("已按显示器开启计算",
                         f"已恢复计入显示器功耗 {mon_w:.0f} W。")
        else:
            self._notify("显示器状态：自动检测",
                         "系统将依据空闲时长自动判断是否已熄屏。")
        try:
            self._refresh_readout()
        except Exception:
            pass
        self._save_session()

    def _disp_menu_set(self, state):
        """托盘菜单切换显示器状态：None=自动 / True=开 / False=关。"""
        self._set_display_manual(state)
        self._sync_disp_menu()

    def _sync_disp_menu(self):
        """同步托盘菜单三态勾选（is 比较以区分 False 与 None）。"""
        m = getattr(self, "_disp_manual", None)
        for _a, _v in ((getattr(self, "a_disp_auto", None), None),
                       (getattr(self, "a_disp_on", None), True),
                       (getattr(self, "a_disp_off", None), False)):
            if _a is not None:
                _a.setChecked(m is _v)

    def on_sample(self, data: dict):
        now = time.time()
        # v18.9 跨零点自动生成前一日日报
        try:
            self._check_daily_rollover()
        except Exception:
            pass
        disp_on = self._display_on()
        self.display_on = disp_on
        est = PM.estimate(self.model, data["cpu_load"], data["gpu_power"],
                          data["gpu_valid"], disp_on)
        self.cur = {
            "cpu_load": data["cpu_load"], "gpu_power": data["gpu_power"],
            "gpu_valid": data["gpu_valid"], "wall": est["wall_watts"],
            "sys": est["sys_watts"], "breakdown": est["breakdown"],
            "display_on": disp_on,
            "psu_eff": est.get("psu_eff", self.model.psu_efficiency),
        }
        # v18 系统信息侧栏动态数据
        if data.get("sys"):
            self._sys_dyn = data["sys"]
        # v18.6 托盘悬停实时功率（后台常驻时鼠标移到托盘即可看当前功耗）
        if getattr(self, "tray", None) is not None:
            try:
                self.tray.setToolTip(
                    f"PC 用电电费计算器 {APP_VERSION}\n"
                    f"当前功率 {est['wall_watts']:.0f} W · 累计 {self.energy_wh/1000.0:.3f} kWh")
            except Exception:
                pass
        # v18.8 托盘图标直接显示功率数字（每 3 拍≈6s 换一次图标，避免频繁重绘）
        self._tray_tick = getattr(self, "_tray_tick", 0) + 1
        if getattr(self, "tray", None) is not None and self._tray_tick % 3 == 1:
            try:
                self.tray.setIcon(QIcon(self._tray_icon_pixmap(est["wall_watts"])))
            except Exception:
                pass
        # v18.8 迷你悬浮窗刷新
        if getattr(self, "_mini_visible", False) and getattr(self, "mini", None) is not None:
            self._update_mini()
        # 累计
        if self.running and self.last_ts is not None:
            dt_ms = (now - self.last_ts) * 1000.0
            if dt_ms > 0 and dt_ms < 60000:   # 防跳变
                e = est["wall_watts"] * dt_ms / 3600.0 / 1000.0
                self.energy_wh += e
                self.period_energy_wh[self._period_of(now)] += e
                self.running_elapsed_ms += dt_ms
                self.peak_wall = max(self.peak_wall, est["wall_watts"])
                if not disp_on:
                    # 熄屏期间显示器不再耗电，累计「省下」的电量
                    self._disp_saved_wh += (
                        float(self.model.components.get("显示器", 0.0) or 0.0)
                        * dt_ms / 3600000.0)
                self.all_samples.append((now, est["wall_watts"], est["sys_watts"],
                                         data["cpu_load"], data["gpu_power"] or 0.0))
                hk = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:00")
                b = self.hourly.setdefault(hk, [0.0, 0]); b[0] += est["wall_watts"]; b[1] += 1
                # 软件耗电：按各进程 CPU 时间增量占比，分摊本周期 CPU 估算功耗
                apps = data.get("apps")
                if apps and apps[0] > self.app_cpu_snap[0]:
                    _pts, prev_map = self.app_cpu_snap
                    cur_ts, cur_map = apps
                    if prev_map:
                        cpu_w = float((est.get("breakdown") or {}).get("CPU", 0.0) or 0.0)
                        tot = 0.0
                        deltas = {}
                        for nm, sec in cur_map.items():
                            d = sec - prev_map.get(nm, sec)   # 新进程从当前值起算，不倒算历史
                            if d > 0.01:
                                deltas[nm] = d
                                tot += d
                        if tot > 0.001 and cpu_w > 0:
                            cpu_wh_tick = cpu_w * (dt_ms / 1000.0) / 3600.0
                            for nm, d in deltas.items():
                                self.app_cpu_wh[nm] = self.app_cpu_wh.get(nm, 0.0) + cpu_wh_tick * d / tot
                    # 快照推进必须在 if prev_map 之外：首轮也要落库，否则永远空对比
                    self.app_cpu_snap = (cur_ts, cur_map)
                # 待机识别：CPU 低载且 GPU 低功耗（或核显）算空闲时段
                is_idle = (data["cpu_load"] <= self.idle_cpu_thresh and
                           (not data["gpu_valid"] or (data["gpu_power"] or 0.0) <= self.idle_gpu_thresh))
                if is_idle:
                    self.idle_energy_wh += e
                    self.idle_streak_ms += dt_ms
                    self.idle_streak_energy_wh += e
                else:
                    self.active_energy_wh += e
                    self.idle_streak_ms = 0.0
                    self.idle_streak_energy_wh = 0.0
                    self._idle_nudged = False
                # 待机自动提醒：连续空闲达阈值且未提醒过，弹通知建议睡眠/关机
                if (self.idle_nudge_enabled and is_idle and not self._idle_nudged
                        and self.idle_streak_ms >= self.idle_nudge_min * 60000.0):
                    self._idle_nudged = True
                    wasted = self.idle_streak_energy_wh / 1000.0
                    kwh_total = self.energy_wh / 1000.0
                    blended = (self._current_cost() / kwh_total) if kwh_total > 0 else self.rate
                    self._notify("电脑长时间空闲",
                                 f"已空闲约 {self.idle_nudge_min:.0f} 分钟，期间空耗约 "
                                 f"{wasted:.3f} kWh（¥{wasted * blended:.2f}）。"
                                 f"建议睡眠或关机以省电。")
        self.last_ts = now

        # v18.11 周期落盘：此前 session 只在设置变更/导出/退出时写入，
        # 程序被强杀或崩溃会丢失全部累计电量与采样。每 30 拍（约 60s）存一次。
        self._save_tick = getattr(self, "_save_tick", 0) + 1
        if self._save_tick % 30 == 0:
            self._save_session()

        # 功耗超阈值告警（仅在监测中、且一次越限只提示一次）
        if self.alert_enabled and self.running:
            if self.cur["wall"] > self.alert_threshold and not self._alert_active:
                self._alert_active = True
                self._notify("功耗超阈值",
                             f"当前插座功耗 {self.cur['wall']:.0f}W 已超过阈值 {self.alert_threshold:.0f}W")
            elif self.cur["wall"] <= self.alert_threshold:
                self._alert_active = False

        self._refresh_readout()
        self._refresh_chart()
        self._refresh_breakdown()
        self._refresh_countdown()

        # 自动汇总
        if self.running and not self.finished:
            if self.running_elapsed_ms >= self.window_hours * 3600 * 1000:
                self._finish(auto=True)

    def _refresh_readout(self):
        self.wall_big.setText(f"{self.cur['wall']:.1f} W")
        calib_note = ""
        if self.calib_idle > 0 and self.calib_peak > 0:
            calib_note = f" · 已用实测待机{self.calib_idle:.0f}/满载{self.calib_peak:.0f}W校准"
        elif self.calib_k != 1.0:
            calib_note = f" · 已用系数×{self.calib_k:.2f}校准"
        disp_note = ""
        if not getattr(self, "display_on", True):
            _mon = float(getattr(self.model, "components", {}).get("显示器", 0.0) or 0.0)
            disp_note = (f" · 显示器已关（省 {_mon:.0f}W，"
                         f"累计省 {self._disp_saved_wh/1000.0:.3f} 度）")
        # v18.11：填了额定功率就用 80 PLUS 曲线算出的实时效率，否则用固定效率
        _eff_live = self.cur.get("psu_eff")
        if not _eff_live:
            _eff_live = self.model.psu_efficiency
        _eff_note = "（动态）" if float(getattr(self, "psu_rating_w", 0.0) or 0.0) > 0 else ""
        self.wall_sub.setText(
            f"系统功耗 {self.cur['sys']:.1f} W（含电源损耗 / 效率 "
            f"{_eff_live:.2f}{_eff_note}{calib_note}）{disp_note}")
        kwh = self.energy_wh / 1000.0
        self.energy_big.setText(f"{kwh:.3f} kWh")
        self.cost_sub.setText(f"电费 ¥{self._current_cost():.2f}"
                              + ("（峰谷）" if self.price_mode == "峰谷"
                                 else "（阶梯）" if self.price_mode == "阶梯" else ""))
        self.cpu_lbl.setText(f"CPU {self.cur['cpu_load']:.0f}%")
        if self.cur["gpu_valid"] and self.cur["gpu_power"] is not None:
            self.gpu_lbl.setText(f"GPU {self.cur['gpu_power']:.0f} W（真实）")
        else:
            self.gpu_lbl.setText("GPU 估算中")
        # 估算 PSU 建议
        # 峰值只算主机：显示器走市电，不占电源功率；额定功率已知时按峰值负载取动态效率
        _mon_pk = float(getattr(self.model, "monitor_w", 0.0) or 0.0)
        _host_pk = max(1.0, self.model.peak_sys - _mon_pk)
        _eff_pk = PM.psu_eff_at(_host_pk, float(getattr(self, "psu_rating_w", 0.0) or 0.0))
        if not _eff_pk:
            _eff_pk = self.model.psu_efficiency
        rec = _host_pk / _eff_pk * 1.3
        self.psu_hint.setText(
            f"建议电源 ≥ {rec:.0f} W（主机峰值 {_host_pk:.0f}W / 效率 {_eff_pk:.2f} + 30% 余量）")
        # 月度电费预估（按本次已累计平均功耗外推）
        elapsed_h = self.running_elapsed_ms / 3_600_000.0
        if elapsed_h > 0.001 and self.energy_wh > 0:
            avg_w = self.energy_wh / elapsed_h
        else:
            avg_w = self.cur["wall"] if self.cur["wall"] else 0.0
        kwh_day = avg_w * 24.0 / 1000.0
        kwh_month = avg_w * 720.0 / 1000.0
        kwh_total = self.energy_wh / 1000.0
        blended = (self._current_cost() / kwh_total) if kwh_total > 0 else self.rate
        month_cost = kwh_month * blended
        day_cost = kwh_day * blended
        hour_cost = kwh_day / 24.0 * blended
        self.proj_hour.setText(f"¥{hour_cost:,.2f}")
        self.proj_day.setText(f"¥{day_cost:,.2f}")
        self.proj_month.setText(f"¥{month_cost:,.0f}")
        # v18 系统信息侧栏
        self._update_sysinfo()

        # 月度预算进度 + 超阈值预警
        if self.budget_kwh > 0 or self.budget_cost > 0:
            pct_k = (kwh_month / self.budget_kwh * 100.0) if self.budget_kwh > 0 else 0.0
            pct_c = (month_cost / self.budget_cost * 100.0) if self.budget_cost > 0 else 0.0
            pct = max(pct_k, pct_c)
            self.budget_bar.setValue(min(100, int(pct + 0.5)))
            self.budget_big.setText(f"{pct:.0f}%")
            if self.budget_cost > 0 and self.budget_kwh > 0:
                sub = (f"电费¥{month_cost:,.0f}/{self.budget_cost:.0f} · "
                       f"电量{kwh_month:.0f}/{self.budget_kwh:.0f}kWh")
            elif self.budget_cost > 0:
                sub = f"电费¥{month_cost:,.0f}/{self.budget_cost:.0f} · 剩¥{max(0.0, self.budget_cost - month_cost):,.0f}"
            else:
                sub = f"电量{kwh_month:.0f}/{self.budget_kwh:.0f}kWh"
            self.budget_sub.setText(sub)
            col = "#2fae6b" if pct < 80 else ("#ff9f1c" if pct < 100 else "#ff5b6e")
            if col != self._budget_col:
                self._budget_col = col
                self.budget_bar.setStyleSheet(
                    "QProgressBar{border:none;border-radius:5px;background:#eef1f7;}"
                    f"QProgressBar::chunk{{background:{col};border-radius:5px;}}")
            # 预警：达到设定比例且未已提醒则弹通知，低于则复位（单次越限只提示一次）
            if self.budget_alert_enabled and pct >= self.budget_alert_pct and not self._budget_alert_active:
                self._budget_alert_active = True
                self._notify("用电预算预警",
                             f"本月预估用电已达预算的 {pct:.0f}%（电费约 ¥{month_cost:,.0f} / 预算 ¥{self.budget_cost:,.0f}）")
            elif pct < self.budget_alert_pct:
                self._budget_alert_active = False
        else:
            self.budget_big.setText("未设")
            self.budget_bar.setValue(0)
            self.budget_sub.setText("设置里可设月度预算")
            self._budget_alert_active = False

        # 待机占比
        if self.energy_wh > 0:
            idle_ratio = self.idle_energy_wh / self.energy_wh * 100.0
            self.idle_big.setText(f"{idle_ratio:.0f}%")
            self.idle_sub.setText(f"空闲耗 {self.idle_energy_wh/1000.0:.2f} kWh")
            icol = "#2fae6b" if idle_ratio < 30 else ("#ff9f1c" if idle_ratio < 60 else "#ff5b6e")
            if icol != self._idle_col:
                self._idle_col = icol
                self.idle_big.setStyleSheet(f"font-size:34px;font-weight:700;color:{icol};")
        else:
            self.idle_big.setText("—")
            if self._idle_col != "#1f2a44":
                self._idle_col = "#1f2a44"
                self.idle_big.setStyleSheet("font-size:34px;font-weight:700;color:#1f2a44;")
            self.idle_sub.setText("空闲时段耗电")

    def _refresh_chart(self):
        """增量更新：只追加新点、淘汰 60 分钟前的旧点，绝不全量重建（修复卡顿）。"""
        now_ms = time.time() * 1000.0
        cutoff = now_ms - 3_600_000.0
        # 1) 追加增量新点（从最新往回找，直到超过已入图的时间戳）
        last = self._chart_pts[-1][0] if self._chart_pts else 0.0
        add = []
        for s in reversed(self.all_samples):
            ts_ms = s[0] * 1000.0
            if ts_ms <= last:
                break
            add.append((ts_ms, s[1]))
        for ts_ms, w in reversed(add):
            self._chart_pts.append((ts_ms, w))
            self.series.append(ts_ms, w)
        # 2) 淘汰窗口外旧点
        drop = 0
        for ts_ms, _w in self._chart_pts:
            if ts_ms < cutoff:
                drop += 1
            else:
                break
        if drop:
            for _ in range(drop):
                self._chart_pts.popleft()
            try:
                self.series.removePoints(0, drop)
            except Exception:            # 兜底：极少数环境无 removePoints
                self.series.clear()
                for ts_ms, w in self._chart_pts:
                    self.series.append(ts_ms, w)
        # 3) x 轴范围按 15s 快照去抖，y 轴量程变化去抖，减少无谓重排版
        xb = int(now_ms // 15000)
        if xb != self._chart_xbucket:
            self._chart_xbucket = xb
            self.axis_x.setRange(QDateTime.fromMSecsSinceEpoch(int(cutoff)),
                                 QDateTime.fromMSecsSinceEpoch(int(now_ms)))
        if self._chart_pts:
            maxw = max(w for _ts, w in self._chart_pts)
            mb = int(maxw * 1.15 // 25)
            if mb != self._chart_max_bucket:
                self._chart_max_bucket = mb
                self.axis_y.setRange(0, max(100.0, maxw * 1.15))

    def _refresh_countdown(self):
        remain_ms = max(0.0, self.window_hours * 3600 * 1000 - self.running_elapsed_ms)
        td = timedelta(milliseconds=remain_ms)
        h = int(td.total_seconds() // 3600)
        m = int((td.total_seconds() % 3600) // 60)
        s = int(td.total_seconds() % 60)
        self.cd_big.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def _period_of(self, epoch_s: float) -> str:
        """按本地小时划分峰谷，时段边界可自定义（self.tou_valley / self.tou_peak）。"""
        h = datetime.fromtimestamp(epoch_s).hour
        vs, ve = self.tou_valley
        if vs <= ve:
            if vs <= h < ve:
                return "谷"
        else:  # 跨午夜，如 23→7
            if h >= vs or h < ve:
                return "谷"
        for ps, pe in self.tou_peak:
            if ps <= h < pe:
                return "峰"
        return "平"

    def _tou_window_desc(self) -> str:
        vs, ve = self.tou_valley
        vdesc = f"{vs}-{ve}点" if vs <= ve else f"{vs}→{ve}点"
        pdescs = [f"{ps}-{pe}" for ps, pe in self.tou_peak if ps < pe]
        return f"谷 {vdesc} · 峰 {'/'.join(pdescs) if pdescs else '无'} · 平 其余"

    def _current_cost(self) -> float:
        """当前累计电费：按所选计费方式（单一/峰谷/阶梯）计算。"""
        if self.price_mode == "峰谷":
            return (self.period_energy_wh["谷"] / 1000.0 * self.rate_valley
                    + self.period_energy_wh["平"] / 1000.0 * self.rate_flat
                    + self.period_energy_wh["峰"] / 1000.0 * self.rate_peak)
        if self.price_mode == "阶梯":
            total = self.tier_base + self.energy_wh / 1000.0
            return self._tiered_cost(total) - self._tiered_cost(self.tier_base)
        return self.energy_wh / 1000.0 * self.rate

    def _tiered_cost(self, kwh: float) -> float:
        """对某总用电量(kWh)按阶梯电价求总电费。"""
        if kwh <= 0:
            return 0.0
        c = min(kwh, self.tier_l1) * self.tier_r1
        if kwh > self.tier_l1:
            c += (min(kwh, self.tier_l2) - self.tier_l1) * self.tier_r2
        if kwh > self.tier_l2:
            c += (kwh - self.tier_l2) * self.tier_r3
        return c

    def _refresh_breakdown(self):
        bd = self.cur.get("breakdown", {})
        self.table.setRowCount(0)
        for name, w in bd.items():
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(name)))
            self.table.setItem(r, 1, QTableWidgetItem(f"{w:.1f}"))

    # ---------------- 控制 ----------------
    def toggle_run(self):
        """v18 保留为托盘「暂停/继续」入口；窗口按钮已移除（始终监测）。"""
        if self.finished:
            self.finished = False
        self.running = not self.running
        self.status_lbl.setText("监测中…" if self.running else "已暂停")
        self.last_ts = time.time()
        self._save_session()

    def reset_session(self):
        # 若本次是在监测中途手动重置、且已有累计，先归档再清空（避免数据丢失）
        if not self.finished and self.energy_wh > 0:
            self._archive_current()
        self.energy_wh = 0.0
        self.peak_wall = 0.0
        self.idle_energy_wh = 0.0
        self.active_energy_wh = 0.0
        self.idle_streak_ms = 0.0
        self.idle_streak_energy_wh = 0.0
        self._idle_nudged = False
        self.running_elapsed_ms = 0.0
        self.running = True   # v18 常驻：重置后继续监测
        self.finished = False
        self.last_ts = time.time()
        self.all_samples.clear()
        self.hourly.clear()
        self.app_cpu_snap = (0.0, {})
        self.app_cpu_wh = {}
        self._chart_pts.clear()
        self.series.clear()
        self._chart_xbucket = -1
        self._chart_max_bucket = -1
        self.status_lbl.setText("已重置 · 新一轮监测中")
        self._refresh_readout(); self._refresh_chart(); self._refresh_countdown()
        self._save_session()
        # v18.1：不再弹窗——后台自动续轮时弹窗会挂起监测；状态栏已提示

    # ---------------- 电价快捷设置 ----------------
    def eventFilter(self, obj, ev):
        if obj is self.rate_sub and ev.type() == QEvent.Type.MouseButtonPress:
            self.quick_edit_rate()
            return True
        return super().eventFilter(obj, ev)

    def quick_edit_rate(self):
        """主界面点击电价 → 直接改「每度电多少钱」；按当前计费方式定位到对应单价。"""
        mode = self.price_mode
        if mode == "单一":
            cur, where = self.rate, "单一电价"
        elif mode == "峰谷":
            p = self._period_of(time.time())
            cur = {"谷": self.rate_valley, "平": self.rate_flat, "峰": self.rate_peak}[p]
            where = f"峰谷电价 · 当前处于【{p}】时段"
        else:
            kwh = self.energy_wh / 1000.0
            if kwh > self.tier_l2:
                cur, where = self.tier_r3, f"阶梯第三档（>{self.tier_l2:.0f} kWh）"
            elif kwh > self.tier_l1:
                cur, where = self.tier_r2, f"阶梯第二档（{self.tier_l1:.0f}~{self.tier_l2:.0f} kWh）"
            else:
                cur, where = self.tier_r1, f"阶梯第一档（≤{self.tier_l1:.0f} kWh）"
        val, ok = QInputDialog.getDouble(self, "每度电价格",
                                         f"{where}\n请输入单价（元/度）：",
                                         cur, 0.01, 20.0, 3)
        if not ok:
            return
        if mode == "单一":
            self.rate = val
        elif mode == "峰谷":
            p = self._period_of(time.time())
            if p == "谷":
                self.rate_valley = val
            elif p == "平":
                self.rate_flat = val
            else:
                self.rate_peak = val
        else:
            kwh = self.energy_wh / 1000.0
            if kwh > self.tier_l2:
                self.tier_r3 = val
            elif kwh > self.tier_l1:
                self.tier_r2 = val
            else:
                self.tier_r1 = val
        self.rate_sub.setText(f"电价 {self.rate:.2f} 元/度 ▸ 点击改价"
                              + (f" · 计费方式：{self.price_mode}" if self.price_mode != "单一" else ""))
        self._refresh_readout()
        self._save_session()

    # ---------------- 设置（v18.7：集成到主界面右侧停靠面板，不再弹模态对话框） ----------------
    def open_settings(self):
        # 点「设置」展开/收起抽屉式浮层；展开时把控件同步为当前生效值
        if self._settings_dock.isVisible():
            self._settings_dock.hide()
        else:
            self._sync_settings_widgets()
            self._reposition_settings_dock()
            self._settings_dock.show()
            self._settings_dock.raise_()

    def _reposition_settings_dock(self):
        """浮层贴主界面右缘、上下通高。"""
        parent = self._settings_dock.parentWidget()
        if parent is not None:
            w = self._settings_dock.width()
            self._settings_dock.setGeometry(parent.width() - w, 0, w, parent.height())

    def resizeEvent(self, ev):
        # v18.9 窗口尺寸变化时保持设置浮层贴右缘
        if getattr(self, "_settings_dock", None) is not None and self._settings_dock.isVisible():
            self._reposition_settings_dock()
        super().resizeEvent(ev)

    def keyPressEvent(self, ev):
        # v18.10 Esc 收起设置浮层
        if ev.key() == Qt.Key.Key_Escape and self._settings_dock.isVisible():
            self._settings_dock.hide()
            return
        super().keyPressEvent(ev)

    def _make_yesterday_report(self):
        """v18.10 托盘手动生成昨日日报（无数据时托盘提示）。"""
        yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        out = self._daily_report(yest)
        if not out:
            self._notify("暂无数据", f"{yest} 没有用电记录，无法生成日报")

    def _sync_settings_widgets(self):
        s = self._sw
        s["rate"].setValue(self.rate); s["win"].setValue(self.window_hours)
        s["eff"].setValue(self.psu_eff); s["samp"].setValue(self.sample_ms)
        if "pr" in s:
            s["pr"].setValue(int(getattr(self, "psu_rating_w", 0.0) or 0.0))
        s["mode"].setCurrentText(self.price_mode)
        s["tbase"].setValue(self.tier_base); s["tl1"].setValue(self.tier_l1); s["tr1"].setValue(self.tier_r1)
        s["tl2"].setValue(self.tier_l2); s["tr2"].setValue(self.tier_r2); s["tr3"].setValue(self.tier_r3)
        s["rv"].setValue(self.rate_valley); s["rf"].setValue(self.rate_flat); s["rp"].setValue(self.rate_peak)
        s["vsb"].setValue(self.tou_valley[0]); s["veb"].setValue(self.tou_valley[1])
        for i, keys in enumerate((("p1s", "p1e"), ("p2s", "p2e"))):
            pk = self.tou_peak[i] if len(self.tou_peak) > i else (0, 0)
            s[keys[0]].setValue(pk[0]); s[keys[1]].setValue(pk[1])
        s["al"].setChecked(self.alert_enabled); s["alth"].setValue(self.alert_threshold)
        s["bk"].setValue(self.budget_kwh); s["bc"].setValue(self.budget_cost)
        s["bap"].setValue(self.budget_alert_pct); s["bal"].setChecked(self.budget_alert_enabled)
        s["ict"].setValue(self.idle_cpu_thresh); s["igt"].setValue(self.idle_gpu_thresh)
        s["inud"].setChecked(self.idle_nudge_enabled); s["inm"].setValue(self.idle_nudge_min)
        s["au"].setChecked(self.autostart); s["aum"].setChecked(self.autostart_monitor)
        s["ck"].setValue(self.calib_k); s["cidle"].setValue(self.calib_idle); s["cpeak"].setValue(self.calib_peak)
        s["mini"].setChecked(getattr(self, "_mini_visible", False))
        # v18.11 显示器状态三态回显（None=自动 / True=开 / False=关）
        _dcb = s.get("disp")
        if _dcb is not None:
            _dm = getattr(self, "_disp_manual", None)
            _dcb.setCurrentIndex(0 if _dm is None else (1 if _dm is True else 2))

    def _build_settings_panel(self):
        panel = QWidget(); panel.setObjectName("settingsPanel")
        panel.setStyleSheet("QWidget#settingsPanel { background: transparent; } " + CSS)
        vl = QVBoxLayout(panel); vl.setContentsMargins(16, 14, 16, 14); vl.setSpacing(10)
        # v18.10 标题栏：标题 + ✕ 收起按钮
        head = QHBoxLayout()
        cap = QLabel(f"<b>设置</b> · {APP_VERSION}")
        cap.setStyleSheet("font-size:15px;color:#1f2a44;")
        head.addWidget(cap)
        head.addItem(QSpacerItem(20, 10, QSizePolicy.Expanding))
        btn_x = QPushButton("✕"); btn_x.setObjectName("ghost"); btn_x.setFixedWidth(34)
        btn_x.setToolTip("收起设置 (Esc)")
        btn_x.clicked.connect(self._settings_dock.hide)
        head.addWidget(btn_x)
        vl.addLayout(head)
        form_w = QWidget(); fl = QFormLayout(form_w); fl.setSpacing(9)
        rate = QDoubleSpinBox(); rate.setRange(0.1, 5.0); rate.setDecimals(2)
        rate.setValue(self.rate); rate.setSuffix(" 元/度")
        win = QDoubleSpinBox(); win.setRange(0.1, 720); win.setDecimals(1)
        win.setValue(self.window_hours); win.setSuffix(" 小时")
        eff = QDoubleSpinBox(); eff.setRange(0.70, 0.98); eff.setDecimals(2)
        eff.setValue(self.psu_eff)
        # v18.11 电源额定功率：填了才启用动态效率曲线，0 = 不启用
        # v18.13 增加「按推荐填入」——很多人说不清电源铭牌，给个估算起点总好过一直停在 0
        pr = QSpinBox(); pr.setRange(0, 2000); pr.setSingleStep(50)
        pr.setValue(int(getattr(self, "psu_rating_w", 0) or 0)); pr.setSuffix(" W")
        pr.setToolTip("填电源铭牌额定功率（如 500 / 600），将按 80 PLUS 曲线\n"
                      "随负载率动态计算转换效率；填 0 则使用下方固定的电源效率。")
        pr_w = QWidget(); pr_l = QHBoxLayout(pr_w)
        pr_l.setContentsMargins(0, 0, 0, 0); pr_l.setSpacing(6)
        pr_l.addWidget(pr, 1)
        btn_pr = QPushButton("按推荐填入")
        btn_pr.setFixedWidth(86)
        btn_pr.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_pr.setToolTip("按「主机峰值 ÷ 效率 + 30% 余量」估算，取最接近的常见额定档位。\n"
                          "不确定电源铭牌时先填它，日后再按实际铭牌改。")
        btn_pr.clicked.connect(lambda: pr.setValue(self._suggest_psu_rating()))
        pr_l.addWidget(btn_pr)
        samp = QSpinBox(); samp.setRange(1000, 10000); samp.setSingleStep(500)
        samp.setValue(self.sample_ms); samp.setSuffix(" 毫秒")
        # 计费方式
        mode = QComboBox()
        mode.addItems(["单一", "峰谷", "阶梯"])
        mode.setCurrentText(self.price_mode)
        # 阶梯电价
        tbase = QDoubleSpinBox(); tbase.setRange(0, 100000); tbase.setDecimals(0)
        tbase.setValue(self.tier_base); tbase.setSuffix(" kWh")
        tl1 = QDoubleSpinBox(); tl1.setRange(1, 100000); tl1.setDecimals(0)
        tl1.setValue(self.tier_l1); tl1.setSuffix(" kWh")
        tr1 = QDoubleSpinBox(); tr1.setRange(0.10, 5.0); tr1.setDecimals(2)
        tr1.setValue(self.tier_r1); tr1.setSuffix(" 元/度")
        tl2 = QDoubleSpinBox(); tl2.setRange(1, 100000); tl2.setDecimals(0)
        tl2.setValue(self.tier_l2); tl2.setSuffix(" kWh")
        tr2 = QDoubleSpinBox(); tr2.setRange(0.10, 5.0); tr2.setDecimals(2)
        tr2.setValue(self.tier_r2); tr2.setSuffix(" 元/度")
        tr3 = QDoubleSpinBox(); tr3.setRange(0.10, 5.0); tr3.setDecimals(2)
        tr3.setValue(self.tier_r3); tr3.setSuffix(" 元/度")
        # 峰谷分时计费
        rv = QDoubleSpinBox(); rv.setRange(0.10, 5.0); rv.setDecimals(2)
        rv.setValue(self.rate_valley); rv.setSuffix(" 元/度")
        rf = QDoubleSpinBox(); rf.setRange(0.10, 5.0); rf.setDecimals(2)
        rf.setValue(self.rate_flat); rf.setSuffix(" 元/度")
        rp = QDoubleSpinBox(); rp.setRange(0.10, 5.0); rp.setDecimals(2)
        rp.setValue(self.rate_peak); rp.setSuffix(" 元/度")
        # 校准
        ck = QDoubleSpinBox(); ck.setRange(0.50, 1.50); ck.setDecimals(2)
        ck.setValue(self.calib_k); ck.setSuffix(" ×")
        cidle = QDoubleSpinBox(); cidle.setRange(0, 2000); cidle.setDecimals(0)
        cidle.setValue(self.calib_idle); cidle.setSuffix(" W")
        cpeak = QDoubleSpinBox(); cpeak.setRange(0, 3000); cpeak.setDecimals(0)
        cpeak.setValue(self.calib_peak); cpeak.setSuffix(" W")
        fl.addRow("电价", rate); fl.addRow("监测时长", win)
        fl.addRow("电源效率", eff); fl.addRow("电源额定功率", pr_w)
        fl.addRow("采样间隔", samp)
        fl.addRow(QLabel("<b>计费方式</b>"), QLabel(""))
        fl.addRow("  方式", mode)
        fl.addRow(QLabel("<b>阶梯电价（居民月用量分档）</b>"), QLabel(""))
        fl.addRow("  本月已用基数", tbase)
        fl.addRow("  档1 上限 / 电价", tl1)
        fl.addRow("  档1 电价", tr1)
        fl.addRow("  档2 上限 / 电价", tl2)
        fl.addRow("  档2 电价", tr2)
        fl.addRow("  档3 电价", tr3)
        fl.addRow(QLabel("<b>峰谷分时</b>"), QLabel(""))
        fl.addRow("  谷价 (23-7点)", rv)
        fl.addRow("  平价 (其余时段)", rf)
        fl.addRow("  峰价 (8-11/18-21)", rp)
        # 峰谷时段边界（可自定义）
        vsb = QSpinBox(); vsb.setRange(0, 23); vsb.setValue(self.tou_valley[0])
        veb = QSpinBox(); veb.setRange(0, 23); veb.setValue(self.tou_valley[1])
        p1s = QSpinBox(); p1s.setRange(0, 23); p1s.setValue(self.tou_peak[0][0] if len(self.tou_peak) > 0 else 0)
        p1e = QSpinBox(); p1e.setRange(0, 23); p1e.setValue(self.tou_peak[0][1] if len(self.tou_peak) > 0 else 0)
        p2s = QSpinBox(); p2s.setRange(0, 23); p2s.setValue(self.tou_peak[1][0] if len(self.tou_peak) > 1 else 0)
        p2e = QSpinBox(); p2e.setRange(0, 23); p2e.setValue(self.tou_peak[1][1] if len(self.tou_peak) > 1 else 0)
        fl.addRow(QLabel("<b>峰谷时段（小时，可改）</b>"), QLabel(""))
        fl.addRow("  谷 起", vsb); fl.addRow("  谷 止", veb)
        fl.addRow("  峰1 起", p1s); fl.addRow("  峰1 止", p1e)
        fl.addRow("  峰2 起", p2s); fl.addRow("  峰2 止", p2e)
        fl.addRow(QLabel("  （平价=其余时段；起=止表示禁用该段）"), QLabel(""))
        # 功耗告警
        al = QCheckBox("功耗超阈值告警（弹系统通知）")
        al.setChecked(self.alert_enabled)
        alth = QDoubleSpinBox(); alth.setRange(50, 2000); alth.setDecimals(0)
        alth.setValue(self.alert_threshold); alth.setSuffix(" W")
        fl.addRow(QLabel("<b>功耗告警</b>"), QLabel(""))
        fl.addRow("  启用", al)
        fl.addRow("  阈值(插座W)", alth)
        # 月度用电预算
        bk = QDoubleSpinBox(); bk.setRange(0, 100000); bk.setDecimals(0)
        bk.setValue(self.budget_kwh); bk.setSuffix(" kWh")
        bc = QDoubleSpinBox(); bc.setRange(0, 100000); bc.setDecimals(0)
        bc.setValue(self.budget_cost); bc.setSuffix(" ¥")
        bap = QDoubleSpinBox(); bap.setRange(1, 200); bap.setDecimals(0)
        bap.setValue(self.budget_alert_pct); bap.setSuffix(" %")
        bal = QCheckBox("预算达到阈值时弹系统通知")
        bal.setChecked(self.budget_alert_enabled)
        fl.addRow(QLabel("<b>月度用电预算</b>"), QLabel(""))
        fl.addRow("  月度电量预算(kWh, 0=不设)", bk)
        fl.addRow("  月度电费预算(¥, 0=不设)", bc)
        fl.addRow("  预警阈值比例", bap)
        fl.addRow("  启用预算预警", bal)
        # 待机识别
        ict = QDoubleSpinBox(); ict.setRange(0, 50); ict.setDecimals(0)
        ict.setValue(self.idle_cpu_thresh); ict.setSuffix(" %")
        igt = QDoubleSpinBox(); igt.setRange(0, 200); igt.setDecimals(0)
        igt.setValue(self.idle_gpu_thresh); igt.setSuffix(" W")
        fl.addRow(QLabel("<b>待机识别</b>"), QLabel(""))
        fl.addRow("  CPU 负载≤(视为空闲)", ict)
        fl.addRow("  GPU 功耗≤(视为空闲)", igt)
        # 待机自动提醒
        inud = QCheckBox("长时间空闲自动提醒（建议睡眠/关机）")
        inud.setChecked(self.idle_nudge_enabled)
        inm = QDoubleSpinBox(); inm.setRange(1, 120); inm.setDecimals(0)
        inm.setValue(self.idle_nudge_min); inm.setSuffix(" 分钟")
        fl.addRow(QLabel("<b>待机自动提醒</b>"), QLabel(""))
        fl.addRow("  启用提醒", inud)
        fl.addRow("  连续空闲达(分钟)", inm)
        # 开机自启
        au = QCheckBox("开机自启（启动后最小化到托盘）")
        au.setChecked(self.autostart)
        aum = QCheckBox("开机自启后自动开始监测")
        aum.setChecked(self.autostart_monitor)
        fl.addRow(QLabel("<b>开机自启</b>"), QLabel(""))
        fl.addRow("  开机自启", au)
        fl.addRow("  自启即监测", aum)
        # v18.10 迷你悬浮窗开关（与托盘菜单同步）
        mini_ck = QCheckBox("桌面常驻小卡：实时功率 / 本轮电费")
        mini_ck.setChecked(getattr(self, "_mini_visible", False))
        fl.addRow("  迷你悬浮窗", mini_ck)
        # v18.11 显示器状态：手动关屏时扣除显示器功耗，避免虚高计费
        disp_cb = QComboBox()
        disp_cb.addItem("自动检测（按空闲时长判断）", None)
        disp_cb.addItem("记为开启（始终计入显示器）", True)
        disp_cb.addItem("记为关闭（熄屏省电，有操作自动恢复）", False)
        _dm = getattr(self, "_disp_manual", None)
        disp_cb.setCurrentIndex(0 if _dm is None else (1 if _dm is True else 2))
        disp_cb.setToolTip(
            "显示器功耗（约 30W）是否计入总功耗。\n"
            "系统无法感知手动按显示器电源键关屏，需在此手动标记；\n"
            "标记为关闭后，一旦检测到键鼠操作会自动恢复按开启计。")
        fl.addRow("  显示器状态", disp_cb)
        fl.addRow(QLabel("<b>精度校准</b>"), QLabel(""))
        fl.addRow("单系数倍率", ck)
        fl.addRow("实测待机功耗", cidle)
        fl.addRow("实测满载功耗", cpeak)
        save = QPushButton("保存设置"); save.clicked.connect(self._save_settings_panel)
        row = QHBoxLayout(); row.addItem(QSpacerItem(20, 10, QSizePolicy.Expanding))
        row.addWidget(save); fl.addRow(row)
        vl.addWidget(form_w); vl.addStretch(1)
        # 控件登记（_sync_settings_widgets / _save_settings_panel 读写用）
        self._sw = {"rate": rate, "win": win, "eff": eff, "pr": pr, "samp": samp, "mode": mode,
                    "tbase": tbase, "tl1": tl1, "tr1": tr1, "tl2": tl2, "tr2": tr2, "tr3": tr3,
                    "rv": rv, "rf": rf, "rp": rp, "vsb": vsb, "veb": veb,
                    "p1s": p1s, "p1e": p1e, "p2s": p2s, "p2e": p2e,
                    "al": al, "alth": alth, "bk": bk, "bc": bc, "bap": bap, "bal": bal,
                    "ict": ict, "igt": igt, "inud": inud, "inm": inm,
                    "au": au, "aum": aum, "ck": ck, "cidle": cidle, "cpeak": cpeak,
                    "mini": mini_ck, "disp": disp_cb}
        return panel

    def _save_settings_panel(self):
        s = self._sw
        rate, win, eff, samp, mode = s["rate"], s["win"], s["eff"], s["samp"], s["mode"]
        tbase, tl1, tr1, tl2, tr2, tr3 = s["tbase"], s["tl1"], s["tr1"], s["tl2"], s["tr2"], s["tr3"]
        rv, rf, rp = s["rv"], s["rf"], s["rp"]
        vsb, veb, p1s, p1e, p2s, p2e = s["vsb"], s["veb"], s["p1s"], s["p1e"], s["p2s"], s["p2e"]
        al, alth, bk, bc, bap, bal = s["al"], s["alth"], s["bk"], s["bc"], s["bap"], s["bal"]
        ict, igt, inud, inm = s["ict"], s["igt"], s["inud"], s["inm"]
        au, aum, ck, cidle, cpeak = s["au"], s["aum"], s["ck"], s["cidle"], s["cpeak"]
        if True:   # 对齐原对话框「确认」分支的缩进层级（赋值体原样保留）
            self.rate = rate.value()
            self.window_hours = win.value()
            self.psu_eff = eff.value()
            self.model.psu_efficiency = eff.value()
            # v18.11 电源额定功率：>0 启用动态效率曲线，0 沿用上面固定效率
            self.psu_rating_w = float(s["pr"].value() or 0)
            self.model.psu_rating_w = self.psu_rating_w
            self.sample_ms = samp.value()
            self.worker.set_interval(samp.value())
            self.calib_k = ck.value()
            self.calib_idle = cidle.value()
            self.calib_peak = cpeak.value()
            self.model.calib_k = self.calib_k
            self.model.calib_idle = self.calib_idle
            self.model.calib_peak = self.calib_peak
            self.price_mode = mode.currentText()
            self.rate_valley = rv.value()
            self.rate_flat = rf.value()
            self.rate_peak = rp.value()
            self.tier_base = tbase.value()
            self.tier_l1 = tl1.value()
            self.tier_r1 = tr1.value()
            self.tier_l2 = tl2.value()
            self.tier_r2 = tr2.value()
            self.tier_r3 = tr3.value()
            self.tou_valley = (vsb.value(), veb.value())
            peaks = []
            if p1s.value() < p1e.value():
                peaks.append((p1s.value(), p1e.value()))
            if p2s.value() < p2e.value():
                peaks.append((p2s.value(), p2e.value()))
            self.tou_peak = peaks
            self.autostart = au.isChecked()
            self.autostart_monitor = aum.isChecked()
            self._set_autostart(self.autostart)
            self.alert_enabled = al.isChecked()
            self.alert_threshold = alth.value()
            self._alert_active = False
            self.budget_kwh = bk.value()
            self.budget_cost = bc.value()
            self.budget_alert_pct = bap.value()
            self.budget_alert_enabled = bal.isChecked()
            self._budget_alert_active = False
            self.idle_cpu_thresh = ict.value()
            self.idle_gpu_thresh = igt.value()
            self.idle_nudge_enabled = inud.isChecked()
            self.idle_nudge_min = inm.value()
            self._toggle_mini(s["mini"].isChecked())   # v18.10 迷你悬浮窗开关（内部会存会话）
            # v18.11 显示器状态：currentData 可能返回非 bool，做一次收敛
            _dm = s["disp"].currentData()
            self._disp_manual = _dm if isinstance(_dm, bool) else None
            self.display_on = self._display_on()
            self._sync_disp_menu()
            self.rate_sub.setText(f"电价 {self.rate:.2f} 元/度 ▸ 点击改价"
                                  + (f" · 计费方式：{self.price_mode}" if self.price_mode != "单一" else ""))
            self._refresh_readout()
            self._save_session()
            self._settings_dock.hide()          # 保存后自动收起面板
            self.status_lbl.setText("设置已保存 · 立即生效")

    # ---------------- 每日日报（v18.9：跨零点自动汇总前一日） ----------------
    def _check_daily_rollover(self):
        """跨天检测：新的一天第一拍自动生成前一日日报。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_date is None:
            self._last_date = today
            return
        if today != self._last_date:
            try:
                self._daily_report(self._last_date)
            except Exception:
                pass
            self._last_date = today

    def _daily_report(self, date_str: str) -> str:
        """按小时桶汇总某日用电/电费，写 HTML 日报到 BASE_DIR，返回文件路径（无数据返回空串）。"""
        rows = []
        for k, bucket in sorted(self.hourly.items()):
            if not k.startswith(date_str):
                continue
            sw, n = bucket[0], bucket[1]
            if n <= 0:
                continue
            hh = int(k[11:13])
            avg_w = sw / n
            kwh = avg_w / 1000.0                    # 该小时平均功率 × 1h
            try:
                ts = datetime.strptime(k, "%Y-%m-%d %H:00").timestamp()
            except Exception:
                continue
            if self.price_mode == "峰谷":
                period = self._period_of(ts)
                rate = {"谷": self.rate_valley, "平": self.rate_flat,
                        "峰": self.rate_peak}.get(period, self.rate_flat)
            else:
                period = "—"
                rate = self.rate if self.price_mode == "单一" else self.tier_r1
            rows.append((hh, kwh, avg_w, period, rate))
        if not rows:
            return ""
        total_kwh = sum(r[1] for r in rows)
        total_cost = sum(r[1] * r[4] for r in rows)
        avg_w_all = (sum(r[2] for r in rows) / len(rows)) if rows else 0.0
        top = max(rows, key=lambda r: r[2])
        peak_h, peak_w = top[0], top[2]
        if self.price_mode == "峰谷":
            per = {}
            for _hh, _kwh, _w, p, _r in rows:
                per[p] = per.get(p, 0.0) + _kwh
            split = " · ".join(f"{p} {v:.2f} kWh" for p, v in sorted(per.items()))
        else:
            split = f"电价 {self.rate:.2f} 元/度"
        trs = "".join(
            f"<tr><td>{hh:02d}:00-{hh:02d}:59</td><td>{kwh:.3f}</td><td>{avg_w:.0f}</td>"
            f"<td>{period}</td><td>{rate:.2f}</td><td>¥{kwh * rate:.2f}</td></tr>"
            for hh, kwh, avg_w, period, rate in rows)
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>用电日报 {date_str}</title>
<style>body{{font-family:'Microsoft YaHei';background:#f4f6f9;margin:32px}}
h1{{color:#1f2a44;font-size:22px}} .card{{background:#fff;border-radius:12px;border:1px solid #e6e9ef;
padding:20px;margin-bottom:16px}} .big{{font-size:30px;font-weight:700;color:#1f2a44}}
.sub{{color:#8a93a6;font-size:12px}} table{{border-collapse:collapse;width:100%;font-size:13px}}
td,th{{border-bottom:1px solid #eef1f7;padding:7px 10px;text-align:left}} th{{color:#6b7488}}
.num{{color:#c2410c;font-weight:700}}</style></head><body>
<h1>用电日报 · {date_str} <span style="font-size:12px;color:#8a93a6">{APP_VERSION} 自动生成</span></h1>
<div class="card">当日用电 <span class="big">{total_kwh:.2f} kWh</span> ·
电费 <span class="big num">¥{total_cost:,.2f}</span><br>
<span class="sub">平均功率 {avg_w_all:.0f} W · 功率峰值 {peak_w:.0f} W（{peak_h:02d} 点时段） · {split}</span></div>
<div class="card"><table><tr><th>时段</th><th>用电 (kWh)</th><th>均功率 (W)</th><th>峰/谷</th><th>电价</th><th>费用</th></tr>{trs}</table></div>
</body></html>"""
        out = os.path.join(BASE_DIR, f"用电日报_{date_str}.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            if self.tray is not None:
                self.tray.showMessage("每日日报已生成",
                                      f"{date_str}：{total_kwh:.1f} kWh · ¥{total_cost:,.2f}\n{out}",
                                      QSystemTrayIcon.MessageIcon.Information, 4000)
        except Exception:
            pass
        return out

    # ---------------- 汇总 / 导出 ----------------
    def _finish(self, auto: bool):
        self.finished = True
        self.running = False
        self.status_lbl.setText("监测完成 · 已生成汇总")
        self._save_session()
        self._archive_current()   # 归档本次完成的监测
        html = self._build_report_html()
        if auto:
            # v18 后台常驻：到点自动归档、报告落盘、无缝开启新一轮（不弹窗打断）
            try:
                out = os.path.join(BASE_DIR, f"用电报告_{datetime.now().strftime('%Y%m%d_%H%M')}.html")
                with open(out, "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception:
                out = ""
            self.reset_session()   # reset_session 内已置 running=True 并清空累计
            self.status_lbl.setText("上轮已自动归档 · 新一轮监测中…")
            try:
                if self.tray is not None:
                    self.tray.showMessage("监测周期完成",
                                          f"报告已保存：{out}" if out else "上一轮数据已归档，新一轮监测已开始",
                                          QSystemTrayIcon.MessageIcon.Information, 4000)
            except Exception:
                pass
            return
        d = QDialog(self); d.setWindowTitle("24 小时用电汇总"); d.setStyleSheet(CSS)
        d.resize(760, 600)
        vl = QVBoxLayout(d)
        # 只用 QTextBrowser 渲染（简单 HTML 表格足够）。
        # 旧实现引入 QWebEngineView 会迫使 PyInstaller 打包整个 QtWebEngine（100MB+），
        # 是 exe 膨胀到 206MB 的绝对大头，已移除。
        view = QTextBrowser(); view.setHtml(html)
        vl.addWidget(view, 1)
        bt = QPushButton("保存报告(HTML)"); bt.clicked.connect(lambda: self._save_html(html))
        vl.addWidget(bt)
        d.exec()

    def _build_report_html(self) -> str:
        kwh = self.energy_wh / 1000.0
        cost = self._current_cost()
        if self.price_mode == "峰谷":
            pblock = (
                f"<div class='card'><div class='k'>峰谷分时电费明细</div>"
                f"<div style='font-size:12px;color:#8a93a6;margin-bottom:4px;'>{self._tou_window_desc()}</div><table>"
                f"<tr><td>谷 (23-7点)</td><td style='text-align:right'>{self.period_energy_wh['谷']/1000:.3f} kWh × {self.rate_valley:.2f} = ¥{self.period_energy_wh['谷']/1000*self.rate_valley:.2f}</td></tr>"
                f"<tr><td>平 (其余)</td><td style='text-align:right'>{self.period_energy_wh['平']/1000:.3f} kWh × {self.rate_flat:.2f} = ¥{self.period_energy_wh['平']/1000*self.rate_flat:.2f}</td></tr>"
                f"<tr><td>峰 (8-11/18-21)</td><td style='text-align:right'>{self.period_energy_wh['峰']/1000:.3f} kWh × {self.rate_peak:.2f} = ¥{self.period_energy_wh['峰']/1000*self.rate_peak:.2f}</td></tr>"
                f"</table></div>"
            )
        elif self.price_mode == "阶梯":
            total = self.tier_base + kwh
            pblock = (
                f"<div class='card'><div class='k'>阶梯电价电费明细</div>"
                f"<div style='font-size:12px;color:#8a93a6;margin-bottom:4px;'>"
                f"本月基数 {self.tier_base:.0f} kWh + 本次 {kwh:.3f} = 合计 {total:.1f} kWh"
                f"（档1≤{self.tier_l1:.0f}, 档2≤{self.tier_l2:.0f}）</div><table>"
                f"<tr><td>档1 0–{self.tier_l1:.0f} × {self.tier_r1:.2f}</td>"
                f"<td style='text-align:right'>{self.tier_r1:.2f} 元/度</td></tr>"
                f"<tr><td>档2 {self.tier_l1:.0f}–{self.tier_l2:.0f} × {self.tier_r2:.2f}</td>"
                f"<td style='text-align:right'>{self.tier_r2:.2f} 元/度</td></tr>"
                f"<tr><td>档3 &gt; {self.tier_l2:.0f} × {self.tier_r3:.2f}</td>"
                f"<td style='text-align:right'>{self.tier_r3:.2f} 元/度</td></tr>"
                f"<tr><td><b>本次监测电费（增量）</b></td><td style='text-align:right'><b>¥{cost:.2f}</b></td></tr>"
                f"</table></div>"
            )
        else:
            pblock = (
                f"<div class='card'><div class='k'>电费计算</div><div style='font-size:14px;'>"
                f"单一电价 {self.rate:.2f} 元/度 × {kwh:.3f} kWh = ¥{cost:.2f}</div></div>"
            )
        # 用电预估（按平均功耗外推）
        elapsed_h = self.running_elapsed_ms / 3_600_000.0
        avg_w = (self.energy_wh / elapsed_h) if (elapsed_h > 0.001 and self.energy_wh > 0) else self.cur["wall"]
        kwh_day = avg_w * 24.0 / 1000.0
        kwh_month = avg_w * 720.0 / 1000.0
        blended = (cost / kwh) if kwh > 0 else self.rate
        month_cost = kwh_month * blended
        hour_cost = kwh_day / 24.0 * blended
        proj = (f"<div class='card'><div class='k'>用电预估（按平均 {avg_w:.0f}W 外推）</div>"
                f"<div style='font-size:14px;'>每小时 <b>¥{hour_cost:,.2f}</b> · "
                f"每24小时 <b>{kwh_day:.2f} kWh / ¥{kwh_day * blended:,.2f}</b> · "
                f"每月30天 <b>{kwh_month:.1f} kWh / ¥{month_cost:,.0f}</b></div></div>")
        # 待机功耗分析
        idle_kwh = self.idle_energy_wh / 1000.0
        idle_ratio = (idle_kwh / kwh * 100.0) if kwh > 0 else 0.0
        idle_avg_w = (self.idle_energy_wh / elapsed_h) if elapsed_h > 0.001 else 0.0
        idle_month_kwh = idle_avg_w * 720.0 / 1000.0
        idle_month_cost = idle_month_kwh * blended
        idle_sav = (f"若消除待机耗电（均约 {idle_avg_w:.0f}W），预计每月可省约 ¥{idle_month_cost:,.0f}。"
                    f" 提示：人离即睡眠/关机最省电。") if kwh > 0 else "本次累计不足，暂无待机统计。"
        idle_block = (f"<div class='card'><div class='k'>待机功耗分析</div>"
                      f"<div style='font-size:14px;'>空闲时段耗电 <b>{idle_kwh:.3f} kWh</b>"
                      f"（占 <b>{idle_ratio:.0f}%</b>）· 按当前计费约 <b>¥{idle_kwh * blended:.2f}</b></div>"
                      f"<div style='font-size:12px;color:#6b7488;margin-top:4px;'>{idle_sav}</div></div>")
        # 软件耗电 Top10（按 CPU 时间分摊）
        apps_block = ""
        if self.app_cpu_wh:
            kwh_all_r = kwh
            bl_r = (self._current_cost() / kwh_all_r) if kwh_all_r > 0 else self.rate
            tot_wh = sum(self.app_cpu_wh.values())
            rows = "".join(
                f"<tr><td>{name}</td><td style='text-align:right'>{wh/1000.0:.4f}</td>"
                f"<td style='text-align:right'>¥{wh/1000.0*bl_r:.2f}</td>"
                f"<td style='text-align:right'>{wh/tot_wh*100:.1f}%</td></tr>"
                for name, wh in sorted(self.app_cpu_wh.items(), key=lambda x: -x[1])[:10])
            apps_block = (f"<div class='card'><div class='k'>软件耗电 Top10（按 CPU 时间分摊）</div>"
                          f"<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
                          f"<tr style='color:#8a93a6;'><th align='left'>进程</th>"
                          f"<th align='right'>CPU 耗电(kWh)</th><th align='right'>电费</th>"
                          f"<th align='right'>占比</th></tr>{rows}</table>"
                          f"<div style='font-size:11px;color:#8a93a6;margin-top:4px;'>"
                          f"估算方式：按各进程 CPU 占用时间占比分摊 CPU 估算功耗；GPU/磁盘/内存不做分摊。</div></div>")
        avg_w = (self.energy_wh / elapsed_h) if (elapsed_h > 0.001 and self.energy_wh > 0) else (self.cur["wall"] or 0.0)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        if self.calib_idle > 0 and self.calib_peak > 0:
            calib_note = (f"已用实测待机 {self.calib_idle:.0f}W / 满载 {self.calib_peak:.0f}W "
                          f"对模型做两点线性校准，电费已按校准后功耗计算。")
        elif self.calib_k != 1.0:
            calib_note = f"已用单系数 ×{self.calib_k:.2f} 校准，电费按校准后功耗计算。"
        else:
            calib_note = "未做校准，电费按模型估算功耗计算。"
        # 小时柱图
        keys = sorted(self.hourly.keys())
        bars = ""
        if keys:
            maxv = max((self.hourly[k][0] / self.hourly[k][1]) for k in keys if self.hourly[k][1])
            for k in keys:
                s, n = self.hourly[k]
                avg = s / n if n else 0
                pct = (avg / maxv * 100) if maxv else 0
                bars += (f"<div style='margin:4px 0;'><div style='font-size:12px;color:#555;'>{k} "
                         f"· {avg:.0f}W</div><div style='background:#eef1f7;border-radius:4px;'>"
                         f"<div style='width:{pct:.0f}%;background:#2f6bff;height:14px;border-radius:4px;'></div></div></div>")
        bd = self.cur.get("breakdown", {})
        bd_rows = "".join(f"<tr><td>{k}</td><td style='text-align:right'>{v:.1f} W</td></tr>" for k, v in bd.items())
        return f"""
<html><head><meta charset="utf-8"><style>
body{{font-family:'Microsoft YaHei',sans-serif;background:#f4f6f9;color:#1f2a44;margin:0;padding:24px;}}
.h{{font-size:22px;font-weight:700;}} .card{{background:#fff;border-radius:12px;padding:18px;margin:14px 0;
border:1px solid #e6e9ef;}} .k{{color:#8a93a6;font-size:12px;}} .v{{font-size:30px;font-weight:700;}}
.grid{{display:flex;gap:14px;flex-wrap:wrap;}} .box{{flex:1;min-width:150px;background:#fff;border-radius:12px;
padding:16px;border:1px solid #e6e9ef;}} table{{width:100%;border-collapse:collapse;font-size:13px;}}
td{{padding:6px 4px;border-bottom:1px solid #eef1f7;}}
</style></head><body>
<div class="h">PC 用电电费 · 24 小时汇总</div>
<div class="k">生成时间：{now}</div>
<div class="grid">
  <div class="box"><div class="k">累计电量</div><div class="v">{kwh:.3f} kWh</div></div>
  <div class="box"><div class="k">电费（{self.rate:.2f} 元/度）</div><div class="v">¥{cost:.2f}</div></div>
  <div class="box"><div class="k">平均插座功耗</div><div class="v">{avg_w:.0f} W</div></div>
  <div class="box"><div class="k">峰值插座功耗</div><div class="v">{self.peak_wall:.0f} W</div></div>
</div>
<div class="card"><div class="k">本机配置</div>
  <div style="font-size:14px;line-height:1.7;margin-top:6px;">
  CPU：{self.hw.cpu_name}（{self.hw.cpu_cores}C/{self.hw.cpu_threads}T）<br>
  GPU：{self.hw.gpu_name}{(' · 真实功耗' if self.hw.gpu_is_nvidia else ' · 估算')}<br>
  内存：{self.hw.ram_bytes/1e9:.1f} GB · 存储：{', '.join(f'{t} {s:.0f}G' for t,s in self.hw.disks)}<br>
  显示器：{self.hw.monitor_count} 台 · 系统：{self.hw.os_caption}
  </div></div>
<div class="card"><div class="k">功耗构成（当前估算）</div>
  <table>{bd_rows}</table></div>
<div class="card"><div class="k">逐小时平均功耗</div>{bars}</div>
{pblock}
{proj}
{idle_block}
{apps_block}
<div class="k" style="margin-top:10px;">注：台式机无墙插电表，GPU（N 卡）采用 nvidia-smi 真实读数，
CPU 与其余部件按负载/经验模型估算，结果仅供参考。{calib_note}</div>
</body></html>"""

    def _save_html(self, html: str):
        path, _ = QFileDialog.getSaveFileName(self, "保存报告", "PC用电汇总.html", "HTML (*.html)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            QMessageBox.information(self, "已保存", f"报告已保存到：\n{path}")

    def export_report(self):
        html = self._build_report_html()
        self._save_html(html)

    def open_compare(self):
        """同一份累计数据，对比「单一电价」与「峰谷电价」两种方案的电费差异。"""
        kwh = self.energy_wh / 1000.0
        g, p, f = (self.period_energy_wh["谷"] / 1000.0, self.period_energy_wh["平"] / 1000.0,
                   self.period_energy_wh["峰"] / 1000.0)
        single = kwh * self.rate
        tou = g * self.rate_valley + p * self.rate_flat + f * self.rate_peak
        diff = single - tou

        d = QDialog(self)
        d.setWindowTitle("电价方案对比")
        d.setStyleSheet(CSS)
        d.resize(480, 330)
        vl = QVBoxLayout(d)
        info = QLabel(f"累计电量 {kwh:.3f} kWh　（谷 {g:.3f} / 平 {p:.3f} / 峰 {f:.3f}）")
        info.setStyleSheet("font-size:13px;color:#2b3552;")
        vl.addWidget(info)

        tbl = QTableWidget(2, 3)
        tbl.setHorizontalHeaderLabels(["方案", "计算方式", "电费"])
        tbl.verticalHeader().hide()
        tbl.setItem(0, 0, QTableWidgetItem(f"单一电价 {self.rate:.2f} 元/度"))
        tbl.setItem(0, 1, QTableWidgetItem(f"{kwh:.3f} × {self.rate:.2f}"))
        tbl.setItem(0, 2, QTableWidgetItem(f"¥{single:.2f}"))
        tbl.setItem(1, 0, QTableWidgetItem(f"峰谷电价 谷{self.rate_valley:.2f}/平{self.rate_flat:.2f}/峰{self.rate_peak:.2f}"))
        tbl.setItem(1, 1, QTableWidgetItem("分时段电量 × 各时段电价"))
        tbl.setItem(1, 2, QTableWidgetItem(f"¥{tou:.2f}"))
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        vl.addWidget(tbl)

        if abs(diff) < 0.005:
            concl = "两种方案电费基本一致。"
        elif diff > 0:
            concl = f"改用峰谷电价可省 ¥{diff:.2f}（约 {diff / single * 100:.1f}%）。"
        else:
            concl = f"当前单一电价更划算，峰谷反而多花 ¥{-diff:.2f}。"
        res = QLabel(concl)
        res.setStyleSheet("font-size:14px;font-weight:600;color:#2f6bff;")
        vl.addWidget(res)
        note = QLabel("说明：基于本次已累计的分时段电量计算，切换计费方式不会丢失原始采样数据。")
        note.setStyleSheet("font-size:12px;color:#8a93a6;")
        note.setWordWrap(True)
        vl.addWidget(note)
        ok = QPushButton("关闭")
        ok.clicked.connect(d.accept)
        vl.addWidget(ok)
        d.exec()

    def export_csv(self):
        if not self.all_samples:
            QMessageBox.information(self, "暂无数据", "还没有采样数据，请先开始监测。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出CSV", "PC用电采样.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write("时间,插座功耗W,系统功耗W,CPU负载%,GPU功耗W\n")
                for epoch, wall, sys_w, cpu, gpu in self.all_samples:
                    ts = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"{ts},{wall:.1f},{sys_w:.1f},{cpu:.1f},{gpu:.1f}\n")
            QMessageBox.information(self, "已导出", f"原始采样已保存到：\n{path}（共 {len(self.all_samples)} 行）")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    # ---------------- 会话持久化 ----------------
    def _save_session(self):
        try:
            data = {
                "energy_wh": self.energy_wh, "peak_wall": self.peak_wall,
                "running_elapsed_ms": self.running_elapsed_ms, "running": self.running,
                "finished": self.finished,                 "rate": self.rate, "window_hours": self.window_hours,
                "psu_eff": self.psu_eff, "sample_ms": self.sample_ms,
                "psu_rating_w": float(getattr(self, "psu_rating_w", 0.0) or 0.0),
                "calib_k": self.calib_k, "calib_idle": self.calib_idle,
                "calib_peak": self.calib_peak,
                "price_mode": self.price_mode, "rate_valley": self.rate_valley,
                "rate_flat": self.rate_flat, "rate_peak": self.rate_peak,
                "tier_base": self.tier_base, "tier_l1": self.tier_l1, "tier_r1": self.tier_r1,
                "tier_l2": self.tier_l2, "tier_r2": self.tier_r2, "tier_r3": self.tier_r3,
                "period_energy_wh": self.period_energy_wh,
                "autostart_monitor": self.autostart_monitor,
                "tou_valley": list(self.tou_valley), "tou_peak": [list(x) for x in self.tou_peak],
                "alert_enabled": self.alert_enabled, "alert_threshold": self.alert_threshold,
                "budget_kwh": self.budget_kwh, "budget_cost": self.budget_cost,
                "budget_alert_enabled": self.budget_alert_enabled,
                "budget_alert_pct": self.budget_alert_pct,
                "idle_cpu_thresh": self.idle_cpu_thresh,
                "idle_gpu_thresh": self.idle_gpu_thresh,
                "idle_nudge_enabled": self.idle_nudge_enabled,
                "idle_nudge_min": self.idle_nudge_min,
                "idle_energy_wh": self.idle_energy_wh,
                "active_energy_wh": self.active_energy_wh,
                "ts": time.time(),
                "hourly": self.hourly,
                "app_cpu_wh": self.app_cpu_wh,
                "mini_visible": self._mini_visible,
                "mini_pos": ([self.mini.x(), self.mini.y()]
                             if getattr(self, "mini", None) is not None else self._mini_pos),
                # v18.11 显示器状态与熄屏省电累计
                "disp_manual": self._disp_manual,
                "disp_saved_wh": self._disp_saved_wh,
                "samples": list(self.all_samples)[-2000:],
            }
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    def _load_session_maybe(self):
        if not os.path.exists(SESSION_FILE):
            return
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return
        age = time.time() - d.get("ts", 0)
        if age > (d.get("window_hours", WINDOW_HOURS) * 3600 + 3600):
            return  # 过期，不续算
        # v18：无论 finished 与否都加载配置；finished 会话在末尾重置累计、自动开新一轮
        was_finished = bool(d.get("finished"))
        # 续算
        self.energy_wh = d.get("energy_wh", 0.0)
        self.peak_wall = d.get("peak_wall", 0.0)
        self.running_elapsed_ms = d.get("running_elapsed_ms", 0.0)
        self.rate = d.get("rate", DEFAULT_RATE)
        self.window_hours = d.get("window_hours", WINDOW_HOURS)
        self.psu_eff = d.get("psu_eff", PM.PSU_EFFICIENCY)
        self.model.psu_efficiency = self.psu_eff
        # v18.11 电源额定功率（>0 启用 80 PLUS 动态效率曲线）
        self.psu_rating_w = float(d.get("psu_rating_w", 0.0) or 0.0)
        self.model.psu_rating_w = self.psu_rating_w
        self.calib_k = d.get("calib_k", 1.0)
        self.calib_idle = d.get("calib_idle", 0.0)
        self.calib_peak = d.get("calib_peak", 0.0)
        self.model.calib_k = self.calib_k
        self.model.calib_idle = self.calib_idle
        self.model.calib_peak = self.calib_peak
        self.rate_valley = d.get("rate_valley", 0.30)
        self.rate_flat = d.get("rate_flat", 0.56)
        self.rate_peak = d.get("rate_peak", 0.85)
        self.price_mode = d.get("price_mode", "单一")
        self.tier_base = d.get("tier_base", 0.0)
        self.tier_l1 = d.get("tier_l1", 2160.0)
        self.tier_r1 = d.get("tier_r1", 0.56)
        self.tier_l2 = d.get("tier_l2", 4800.0)
        self.tier_r2 = d.get("tier_r2", 0.61)
        self.tier_r3 = d.get("tier_r3", 0.86)
        self.period_energy_wh = d.get("period_energy_wh", {"谷": 0.0, "平": 0.0, "峰": 0.0})
        self.autostart_monitor = d.get("autostart_monitor", False)
        v = d.get("tou_valley")
        if isinstance(v, (list, tuple)) and len(v) == 2:
            self.tou_valley = (int(v[0]), int(v[1]))
        pk = d.get("tou_peak")
        if isinstance(pk, list):
            self.tou_peak = [(int(a), int(b)) for a, b in pk]
        self.alert_enabled = d.get("alert_enabled", False)
        self.alert_threshold = d.get("alert_threshold", 300.0)
        self.budget_kwh = d.get("budget_kwh", 0.0)
        self.budget_cost = d.get("budget_cost", 0.0)
        self.budget_alert_enabled = d.get("budget_alert_enabled", False)
        self.budget_alert_pct = d.get("budget_alert_pct", 90.0)
        self.idle_cpu_thresh = d.get("idle_cpu_thresh", 5.0)
        self.idle_gpu_thresh = d.get("idle_gpu_thresh", 15.0)
        self.idle_nudge_enabled = d.get("idle_nudge_enabled", False)
        self.idle_nudge_min = d.get("idle_nudge_min", 10.0)
        self.idle_energy_wh = d.get("idle_energy_wh", 0.0)
        self.active_energy_wh = d.get("active_energy_wh", 0.0)
        self.hourly = d.get("hourly", {})
        aw = d.get("app_cpu_wh")
        if isinstance(aw, dict):
            self.app_cpu_wh = {str(k): float(v) for k, v in aw.items()
                               if isinstance(v, (int, float)) and v > 0}
        # v18.8 迷你悬浮窗状态
        self._mini_visible = bool(d.get("mini_visible", False))
        # v18.11 显示器状态：dm 为 True/False 表示手动覆盖，缺失或非布尔即自动
        dm = d.get("disp_manual", None)
        self._disp_manual = dm if isinstance(dm, bool) else None
        self._disp_saved_wh = float(d.get("disp_saved_wh", 0.0) or 0.0)
        mp = d.get("mini_pos")
        if isinstance(mp, (list, tuple)) and len(mp) == 2:
            try:
                self._mini_pos = [int(mp[0]), int(mp[1])]
            except Exception:
                self._mini_pos = None
        for s in d.get("samples", []):
            if len(s) >= 5:
                self.all_samples.append(tuple(s[:5]))
            else:
                self.all_samples.append((s[0], s[1] if len(s) > 1 else 0.0, 0.0, 0.0, 0.0))
        self.rate_sub.setText(f"电价 {self.rate:.2f} 元/度 ▸ 点击改价")
        if was_finished:
            # v18 常驻：上轮已完成的数据已在 _finish 归档，直接开新一轮
            self.energy_wh = 0.0
            self.running_elapsed_ms = 0.0
            self.finished = False
            self.status_lbl.setText("上轮已归档 · 新一轮监测中…")
        elif d.get("running"):
            self.running = True
            self.status_lbl.setText("已恢复监测（续算）")
        self._refresh_readout(); self._refresh_chart(); self._refresh_countdown()

    # ---------------- 历史会话归档 ----------------
    def _load_history(self) -> list:
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _archive_current(self):
        """把当前累计的这次监测归档为一条历史记录（用于多会话趋势对比）。"""
        if self.energy_wh <= 0:
            return
        hist = self._load_history()
        elapsed_h = self.running_elapsed_ms / 3_600_000.0
        avg_w = (self.energy_wh / elapsed_h) if elapsed_h > 0.001 else (self.cur["wall"] or 0.0)
        rec = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "ts": time.time(),
            "duration_h": round(elapsed_h, 2),
            "kwh": round(self.energy_wh / 1000.0, 3),
            "cost": round(self._current_cost(), 2),
            "avg_w": round(avg_w, 1),
            "peak_w": round(self.peak_wall, 1),
            "mode": self.price_mode,
            "idle_kwh": round(self.idle_energy_wh / 1000.0, 3),
        }
        hist.append(rec)
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False)
        except Exception:
            pass

    def open_history(self):
        """历史会话趋势：柱状图（用电量 kWh）+ 折线（电费 ¥）双轴对比，下方明细表。"""
        hist = self._load_history()
        d = QDialog(self); d.setWindowTitle("历史会话趋势"); d.setStyleSheet(CSS)
        d.resize(820, 640)
        vl = QVBoxLayout(d); vl.setContentsMargins(16, 14, 16, 14); vl.setSpacing(12)

        if not hist:
            tip = QLabel("暂无历史记录。\n完成一次 24 小时监测（或中途重置有累计电量时）会自动归档一条记录，"
                         "可在本窗口查看多次会话的用电量与电费趋势对比。")
            tip.setWordWrap(True); tip.setStyleSheet("font-size:13px;color:#6b7488;")
            vl.addWidget(tip)
            ok = QPushButton("关闭"); ok.clicked.connect(d.accept); vl.addWidget(ok)
            d.exec(); return

        # 取最近 30 条用于绘图（避免过密）
        recent = hist[-30:]
        cats = [r["date"].replace(" ", "\n") for r in recent]
        # 双轴图：柱=用电量 kWh，折线=电费 ¥
        bar_set = QBarSet("用电量 kWh")
        line_series = QLineSeries()
        line_series.setName("电费 ¥")
        line_series.setColor(QColor("#ff8a3d"))
        line_series.setPointsVisible(True)
        for i, r in enumerate(recent):
            bar_set.append(r["kwh"])
            line_series.append(i, r["cost"])

        bars = QBarSeries()
        bars.append(bar_set)
        bars.setColor(QColor("#2f6bff"))
        chart = QChart()
        chart.addSeries(bars)
        chart.addSeries(line_series)
        chart.legend().setVisible(True)
        chart.setBackgroundVisible(False)

        ax_x = QBarCategoryAxis(); ax_x.append(cats); ax_x.setLabelsAngle(-45)
        ax_y_l = QValueAxis(); ax_y_l.setTitleText("用电量 kWh")
        ax_y_l.setLabelFormat("%.2f")
        max_kwh = max(r["kwh"] for r in recent) if recent else 1
        ax_y_l.setRange(0, max(1.0, max_kwh * 1.2))
        ax_y_r = QValueAxis(); ax_y_r.setTitleText("电费 ¥")
        ax_y_r.setLabelFormat("%.1f")
        max_cost = max(r["cost"] for r in recent) if recent else 1
        ax_y_r.setRange(0, max(1.0, max_cost * 1.2))

        chart.addAxis(ax_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(ax_y_l, Qt.AlignmentFlag.AlignLeft)
        chart.addAxis(ax_y_r, Qt.AlignmentFlag.AlignRight)
        bars.attachAxis(ax_x); bars.attachAxis(ax_y_l)
        line_series.attachAxis(ax_x); line_series.attachAxis(ax_y_r)

        view = QChartView(chart); view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(260)
        vl.addWidget(view, 1)

        # 汇总指标
        tot_kwh = sum(r["kwh"] for r in hist)
        tot_cost = sum(r["cost"] for r in hist)
        avg_kwh = sum(r["kwh"] for r in recent) / len(recent)
        avg_cost = sum(r["cost"] for r in recent) / len(recent)
        summ = QLabel(f"共 {len(hist)} 次会话 · 近期均 {avg_kwh:.2f} kWh / ¥{avg_cost:.2f}　|　"
                      f"累计 {tot_kwh:.2f} kWh · ¥{tot_cost:.2f}")
        summ.setStyleSheet("font-size:13px;color:#2b3552;font-weight:600;")
        vl.addWidget(summ)

        # 明细表
        tbl = QTableWidget(len(hist), 7)
        tbl.setHorizontalHeaderLabels(["时间", "时长(h)", "用电量(kWh)", "电费(¥)", "均功耗(W)", "待机(kWh)", "计费方式"])
        tbl.verticalHeader().hide()
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for ri, r in enumerate(reversed(hist)):   # 最新在上
            tbl.setItem(ri, 0, QTableWidgetItem(r["date"]))
            tbl.setItem(ri, 1, QTableWidgetItem(f"{r['duration_h']:.1f}"))
            tbl.setItem(ri, 2, QTableWidgetItem(f"{r['kwh']:.3f}"))
            tbl.setItem(ri, 3, QTableWidgetItem(f"{r['cost']:.2f}"))
            tbl.setItem(ri, 4, QTableWidgetItem(f"{r.get('avg_w', 0):.0f}"))
            tbl.setItem(ri, 5, QTableWidgetItem(f"{r.get('idle_kwh', 0):.3f}"))
            tbl.setItem(ri, 6, QTableWidgetItem(r.get("mode", "单一")))
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        tbl.setMaximumHeight(200)
        vl.addWidget(tbl)

        # 底部操作
        row = QHBoxLayout()
        row.addItem(QSpacerItem(20, 10, QSizePolicy.Expanding))
        btn_csv = QPushButton("导出CSV"); btn_csv.setObjectName("ghost")
        btn_csv.clicked.connect(lambda: self._export_history_csv(hist))
        btn_img = QPushButton("导出趋势图"); btn_img.setObjectName("ghost")
        btn_img.clicked.connect(lambda: self._export_history_image(view))
        btn_clear = QPushButton("清除全部历史"); btn_clear.setObjectName("danger")
        btn_clear.clicked.connect(lambda: self._clear_history(d))
        ok = QPushButton("关闭"); ok.clicked.connect(d.accept)
        row.addWidget(btn_csv); row.addWidget(btn_img)
        row.addWidget(btn_clear); row.addWidget(ok)
        vl.addLayout(row)
        d.exec()

    # ---------------- 历史导出 ----------------
    def _export_history_csv(self, hist: list):
        if not hist:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出历史CSV", "PC用电历史.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write("时间,时长(h),用电量(kWh),电费(¥),均功耗(W),待机(kWh),计费方式\n")
                for r in hist:
                    f.write(f"{r['date']},{r['duration_h']:.1f},{r['kwh']:.3f},"
                            f"{r['cost']:.2f},{r.get('avg_w', 0):.0f},"
                            f"{r.get('idle_kwh', 0):.3f},{r.get('mode', '单一')}\n")
            QMessageBox.information(self, "已导出", f"历史记录已保存到：\n{path}（共 {len(hist)} 条）")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _export_history_image(self, view):
        path, _ = QFileDialog.getSaveFileName(self, "导出趋势图", "PC用电趋势.png", "PNG (*.png)")
        if not path:
            return
        try:
            pix = view.grab()
            if pix.save(path):
                QMessageBox.information(self, "已导出", f"趋势图已保存到：\n{path}")
            else:
                QMessageBox.warning(self, "导出失败", "图片保存失败，请换一个路径或格式再试。")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _clear_history(self, parent_dialog):
        r = QMessageBox.question(self, "确认清除", "将删除所有历史会话记录，且不可恢复。确定继续？",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            try:
                if os.path.exists(HISTORY_FILE):
                    os.remove(HISTORY_FILE)
            except Exception:
                pass
            parent_dialog.accept()

    # ---------------- 节能情景模拟 ----------------
    def _simulate(self, hours_off: float, target_eff: float) -> dict:
        """基于本次监测的平均功耗与当前计费方式，估算两种节能情景的月省电量/电费。"""
        elapsed_h = max(self.running_elapsed_ms / 3_600_000.0, 1e-9)
        avg_w = (self.energy_wh / elapsed_h) if (self.energy_wh > 0 and elapsed_h > 1e-6) else (self.cur["wall"] or 0.0)
        kwh_total = self.energy_wh / 1000.0
        blended = (self._current_cost() / kwh_total) if kwh_total > 0 else self.rate
        # v18.11：开了动态效率曲线时，用当前实时效率作为基准
        eff_old = self.cur.get("psu_eff") or self.psu_eff
        # 情景1：每天完全关机 hours_off 小时（那段时间零耗电），按当前平均功耗近似
        sa_kwh = avg_w * hours_off / 1000.0 * 30.0
        sa = sa_kwh * blended
        # 情景2：电源效率从 eff_old 提升到 target_eff（系统功耗不变，插座功耗下降）
        wall_month = avg_w * 720.0 / 1000.0
        sys_month = wall_month * eff_old
        sb_kwh = sys_month * (1.0 / eff_old - 1.0 / target_eff) if target_eff > 0 else 0.0
        sb = sb_kwh * blended
        return {"avg_w": avg_w, "blended": blended, "wall_month": wall_month,
                "sa_kwh": sa_kwh, "sa": sa, "sb_kwh": sb_kwh, "sb": sb}

    def open_sim(self):
        """节能情景模拟：输入「每天可关机时长」「目标电源效率」，实时算出月省电量与电费。"""
        d = QDialog(self); d.setWindowTitle("节能情景模拟"); d.setStyleSheet(CSS)
        d.resize(540, 400)
        vl = QVBoxLayout(d); vl.setContentsMargins(16, 14, 16, 14); vl.setSpacing(12)

        inp = QFormLayout(); inp.setSpacing(10)
        hoff = QDoubleSpinBox(); hoff.setRange(0, 24); hoff.setDecimals(1)
        hoff.setValue(8.0); hoff.setSuffix(" 小时/天")
        peff = QDoubleSpinBox(); peff.setRange(0.70, 0.98); peff.setDecimals(2)
        peff.setValue(min(0.95, round(self.psu_eff + 0.05, 2))); peff.setSuffix(" 效率")
        inp.addRow("每天可完全关机/睡眠", hoff)
        inp.addRow("目标电源效率", peff)
        vl.addLayout(inp)

        tbl = QTableWidget(4, 3)
        tbl.setHorizontalHeaderLabels(["情景", "月省电量", "月省电费"])
        tbl.verticalHeader().hide()
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        vl.addWidget(tbl, 1)

        note = QLabel("说明：基于本次监测的平均功耗与当前计费方式估算；情景1 按“那段时间完全零耗电”近似，"
                      "情景2 假设系统功耗不变仅电源损耗下降。结果为近似参考。")
        note.setStyleSheet("font-size:12px;color:#8a93a6;"); note.setWordWrap(True)
        vl.addWidget(note)
        ok = QPushButton("关闭"); ok.clicked.connect(d.accept)
        vl.addWidget(ok)

        def recompute():
            r = self._simulate(hoff.value(), peff.value())
            rows = [
                ("当前基准(月)", f"{r['wall_month']:.1f} kWh", f"¥{r['wall_month'] * r['blended']:,.0f}"),
                (f"情景1 每天关机{hoff.value():.0f}h", f"{r['sa_kwh']:.1f} kWh", f"¥{r['sa']:,.0f}"),
                (f"情景2 电源→{peff.value()*100:.0f}%", f"{r['sb_kwh']:.1f} kWh", f"¥{r['sb']:,.0f}"),
                ("合计可省(近似)", f"{r['sa_kwh'] + r['sb_kwh']:.1f} kWh", f"¥{r['sa'] + r['sb']:,.0f}"),
            ]
            for i, (a, b, c) in enumerate(rows):
                tbl.setItem(i, 0, QTableWidgetItem(a))
                tbl.setItem(i, 1, QTableWidgetItem(b))
                tbl.setItem(i, 2, QTableWidgetItem(c))
            tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

        hoff.valueChanged.connect(lambda: recompute())
        peff.valueChanged.connect(lambda: recompute())
        recompute()
        d.exec()

    # ---------------- 用电时段分布 ----------------
    def _hourly_by_clock(self) -> dict:
        """把 self.hourly（按 'YYYY-MM-DD HH:00' 聚合）折算成 {时钟小时: 平均插座功耗W}。"""
        agg = {}
        for k, (sw, n) in self.hourly.items():
            try:
                hh = int(k[11:13])
            except Exception:
                continue
            a = agg.setdefault(hh, [0.0, 0]); a[0] += sw; a[1] += n
        return {h: (agg[h][0] / agg[h][1] if agg[h][1] else 0.0) for h in sorted(agg)}

    def open_hourly(self):
        """用电时段分布：按一天 0–23 时展示平均插座功耗，柱按峰谷时段着色，定位高耗时段。"""
        d = QDialog(self); d.setWindowTitle("用电时段分布"); d.setStyleSheet(CSS)
        d.resize(820, 460)
        vl = QVBoxLayout(d); vl.setContentsMargins(16, 14, 16, 14); vl.setSpacing(12)

        data = self._hourly_by_clock()
        if not data:
            tip = QLabel("暂无分时段数据。请先开始监测并累计一段时间（最好覆盖一天的不同时段），"
                         "即可看到各时钟小时的平均功耗分布。")
            tip.setWordWrap(True); tip.setStyleSheet("font-size:13px;color:#6b7488;")
            vl.addWidget(tip)
            ok = QPushButton("关闭"); ok.clicked.connect(d.accept); vl.addWidget(ok)
            d.exec(); return

        series = QBarSeries()
        cats = []
        palette = {"谷": "#2fae6b", "平": "#2f6bff", "峰": "#ff9f1c"}
        maxv = max(data.values()) or 1.0
        for hh in sorted(data):
            ep = datetime.now().replace(hour=hh, minute=0, second=0, microsecond=0).timestamp()
            period = self._period_of(ep)
            bs = QBarSet(f"{hh:02d}:00")
            bs.append(data[hh])
            bs.setColor(QColor(palette.get(period, "#2f6bff")))
            series.append(bs)
            cats.append(f"{hh:02d}")
        chart = QChart()
        chart.addSeries(series)
        chart.legend().hide()
        chart.setBackgroundVisible(False)
        ax_x = QBarCategoryAxis(); ax_x.append(cats)
        ax_y = QValueAxis(); ax_y.setTitleText("平均插座功耗 W")
        ax_y.setLabelFormat("%.0f"); ax_y.setRange(0, maxv * 1.15)
        chart.addAxis(ax_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(ax_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(ax_x); series.attachAxis(ax_y)
        view = QChartView(chart); view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setMinimumHeight(240)
        vl.addWidget(view, 1)

        peak_h = max(data, key=data.get)
        peak_ep = datetime.now().replace(hour=peak_h, minute=0, second=0, microsecond=0).timestamp()
        peak_period = self._period_of(peak_ep)
        summ = QLabel(f"峰值时段：{peak_h:02d}:00（{peak_period}时段）平均 {data[peak_h]:.0f}W　|　"
                      f"共采样 {len(data)} 个时段　柱色：绿=谷 蓝=平 橙=峰")
        summ.setStyleSheet("font-size:13px;color:#2b3552;font-weight:600;")
        vl.addWidget(summ)
        ok = QPushButton("关闭"); ok.clicked.connect(d.accept); vl.addWidget(ok)
        d.exec()

    # ---------------- 软件耗电 ----------------
    def open_apps(self):
        d = QDialog(self); d.setWindowTitle("软件耗电（按 CPU 时间分摊估算）"); d.setStyleSheet(CSS)
        d.resize(600, 540)
        vl = QVBoxLayout(d)
        tip = QLabel("按各进程 CPU 占用时间的增量占比，分摊本程序估算的 CPU 功耗。"
                     "显卡功耗显卡驱动不提供按进程数据，仅整机计入、不做分摊；磁盘/内存未计。"
                     "自本次监测开始累计，重置后清零。")
        tip.setWordWrap(True); tip.setStyleSheet("font-size:12px;color:#6b7488;")
        vl.addWidget(tip)
        items = sorted(self.app_cpu_wh.items(), key=lambda x: -x[1])
        kwh_cpu_total = sum(self.app_cpu_wh.values()) / 1000.0
        kwh_all = self.energy_wh / 1000.0
        blended = (self._current_cost() / kwh_all) if kwh_all > 0 else self.rate
        tbl = QTableWidget(len(items), 4)
        tbl.setHorizontalHeaderLabels(["进程", "CPU 耗电(kWh)", "电费(¥)", "占比"])
        tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        tbl.verticalHeader().hide()
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for ri, (name, wh) in enumerate(items):
            tbl.setItem(ri, 0, QTableWidgetItem(name))
            tbl.setItem(ri, 1, QTableWidgetItem(f"{wh/1000.0:.4f}"))
            tbl.setItem(ri, 2, QTableWidgetItem(f"{wh/1000.0*blended:.2f}"))
            tbl.setItem(ri, 3, QTableWidgetItem(f"{wh/sum(self.app_cpu_wh.values())*100:.1f}%"
                                                if sum(self.app_cpu_wh.values()) > 0 else "—"))
        vl.addWidget(tbl, 1)
        summ = QLabel(f"共 {len(items)} 个进程 · CPU 分摊总量 {kwh_cpu_total:.3f} kWh"
                      f"（约 ¥{kwh_cpu_total*blended:.2f}）" if items else "暂无数据，开始监测后自动累计。")
        summ.setStyleSheet("font-size:13px;color:#2b3552;font-weight:600;")
        vl.addWidget(summ)
        ok = QPushButton("关闭"); ok.clicked.connect(d.accept); vl.addWidget(ok)
        d.exec()

    def closeEvent(self, ev):
        # 有托盘且非强制退出：最小化到托盘，监测继续
        if self.tray is not None and not self._force_quit:
            ev.ignore()
            self.hide()
            try:
                self.tray.showMessage("已最小化到托盘", "监测仍在后台运行",
                                      QSystemTrayIcon.MessageIcon.Information, 2500)
            except Exception:
                pass
            return
        self._save_session()
        self.worker.stop()   # 停掉常驻采样进程
        if self.worker.isRunning():
            self.worker.quit()
        super().closeEvent(ev)

    # ---------------- 托盘 / 开机自启 ----------------
    def _tray_icon_pixmap(self, watts: float) -> QPixmap:
        """v18.8 生成带实时功率数字的托盘图标（深蓝圆角块 + 白字瓦数 + 黄字 W）。"""
        pix = QPixmap(64, 64)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.NoPen); p.setBrush(QColor("#1f2a44"))
        p.drawRoundedRect(0, 0, 64, 64, 14, 14)
        p.setPen(QColor("#ffffff"))
        p.setFont(QFont("Arial", 19, QFont.Weight.Bold))
        p.drawText(pix.rect().adjusted(0, -8, 0, -8), Qt.AlignmentFlag.AlignCenter, f"{watts:.0f}")
        p.setPen(QColor("#ffd83d"))
        p.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        p.drawText(pix.rect().adjusted(0, 16, 0, 16), Qt.AlignmentFlag.AlignCenter, "W")
        p.end()
        return pix

    def _update_mini(self):
        """v18.8 刷新迷你悬浮窗内容（功率/状态/本轮累计电费）。"""
        m = self.mini
        if m is None:
            return
        wall = (self.cur or {}).get("wall", 0.0)
        m.lbl_w.setText(f"{wall:.0f} W")
        m.lbl_sub.setText("监测中" if self.running else "已暂停")
        try:
            cost = self._current_cost()
        except Exception:
            cost = 0.0
        m.lbl_cost.setText(f"本轮 {self.energy_wh/1000.0:.3f} kWh · ¥{cost:,.2f}")

    def _toggle_mini(self, on: bool):
        """v18.8 开关迷你悬浮窗（托盘菜单/设置面板/会话恢复共用）。"""
        self._mini_visible = bool(on)
        if self.mini is None:
            self.mini = MiniOverlay()
        if on:
            if self._mini_pos:
                self.mini.move(int(self._mini_pos[0]), int(self._mini_pos[1]))
            self._update_mini()
            self.mini.show()
        else:
            self._mini_pos = [self.mini.x(), self.mini.y()]   # 记住拖动后的位置
            self.mini.hide()
        # v18.10 同步托盘菜单勾选态与设置面板复选框
        try:
            if getattr(self, "a_mini", None) is not None:
                self.a_mini.setChecked(self._mini_visible)
            if getattr(self, "_sw", None) and "mini" in self._sw:
                self._sw["mini"].setChecked(self._mini_visible)
        except Exception:
            pass
        self._save_session()

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(self)
        pix = QPixmap(32, 32)
        pix.fill(QColor("#2f6bff"))
        p = QPainter(pix)
        p.setPen(Qt.white)
        p.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "⚡")
        p.end()
        self.tray.setIcon(QIcon(pix))
        self.tray.setToolTip(f"PC 用电电费计算器 {APP_VERSION}")
        menu = QMenu()
        a_show = menu.addAction("显示窗口"); a_show.triggered.connect(self._show_window)
        a_hide = menu.addAction("隐藏窗口"); a_hide.triggered.connect(self.hide)
        # v18.8 迷你悬浮窗开关
        self.a_mini = menu.addAction("迷你悬浮窗"); self.a_mini.setCheckable(True)
        self.a_mini.triggered.connect(lambda: self._toggle_mini(self.a_mini.isChecked()))
        # v18.10 手动生成昨日日报
        a_daily = menu.addAction("生成昨日日报"); a_daily.triggered.connect(self._make_yesterday_report)
        # v18.11 显示器开关状态：手动关屏时扣除显示器功耗，避免虚高计费
        sub_disp = menu.addMenu("显示器状态")
        self.a_disp_auto = sub_disp.addAction("自动检测")
        self.a_disp_on = sub_disp.addAction("记为开启")
        self.a_disp_off = sub_disp.addAction("记为关闭 · 熄屏省电")
        for _a in (self.a_disp_auto, self.a_disp_on, self.a_disp_off):
            _a.setCheckable(True)
        self.a_disp_auto.triggered.connect(lambda: self._disp_menu_set(None))
        self.a_disp_on.triggered.connect(lambda: self._disp_menu_set(True))
        self.a_disp_off.triggered.connect(lambda: self._disp_menu_set(False))
        self._sync_disp_menu()
        menu.addSeparator()
        a_toggle = menu.addAction("开始 / 暂停监测"); a_toggle.triggered.connect(self.toggle_run)
        a_csv = menu.addAction("导出 CSV"); a_csv.triggered.connect(self.export_csv)
        a_report = menu.addAction("导出报告"); a_report.triggered.connect(self.export_report)
        a_hist = menu.addAction("历史趋势"); a_hist.triggered.connect(self.open_history)
        a_sim = menu.addAction("节能模拟"); a_sim.triggered.connect(self.open_sim)
        a_apps = menu.addAction("软件耗电"); a_apps.triggered.connect(self.open_apps)
        a_hourly = menu.addAction("时段分布"); a_hourly.triggered.connect(self.open_hourly)
        menu.addSeparator()
        a_quit = menu.addAction("退出"); a_quit.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activate)
        self.tray.show()

    def _show_window(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def _notify(self, title: str, body: str):
        try:
            if self.tray is not None:
                self.tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Warning, 4000)
            else:
                QMessageBox.warning(self, title, body)
        except Exception:
            pass

    def _on_tray_activate(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            if self.isVisible():
                self.hide()
            else:
                self._show_window()

    def _quit(self):
        self._force_quit = True
        if not self.finished and self.energy_wh > 0:
            self._archive_current()   # 退出时若有未归档的在监测数据则归档
        self._save_session()
        QApplication.instance().quit()

    def _read_autostart(self) -> bool:
        if sys.platform.startswith("win"):
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                     r"Software\Microsoft\Windows\CurrentVersion\Run")
                winreg.QueryValueEx(key, "PC用电电费计算器")
                return True
            except Exception:
                return False
        return False

    def _set_autostart(self, want: bool):
        if not sys.platform.startswith("win"):
            return
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_SET_VALUE)
            if want:
                exe = os.path.abspath(sys.executable)
                winreg.SetValueEx(key, "PC用电电费计算器", 0, winreg.REG_SZ, f'"{exe}" --minimized')
            else:
                try:
                    winreg.DeleteValue(key, "PC用电电费计算器")
                except FileNotFoundError:
                    pass
        except Exception as e:
            QMessageBox.warning(self, "开机自启设置失败", str(e))


def _acquire_single_instance(mutex_name: str = "PC用电电费计算器_SingleInstance") -> bool:
    """v18.11 单实例保护：互斥体 + 心跳双重判定。

    互斥体只能说明「曾经有进程持有过」，不能说明持有者还活着：
    强杀残留的僵尸进程会一直持有互斥体却无法被清理，若只看互斥体，
    后续每次启动都会被误判为「已在运行」，弹出模态框后阻塞（无人点击）
    ——程序看似启动却完全不工作。心跳过期即判定持有者已死，允许接管。

    v18.13：光看心跳新鲜度还不够。刚被关掉的实例心跳同样是新鲜的，
    45 秒内重启会被自己拦在门外（实测复现）。所以再加一道：
    心跳里记的 PID 必须真的还活着，才算「确有实例在跑」。
    """
    if sys.platform != "win32":
        _beat()
        return True
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, mutex_name)
    held = k32.GetLastError() == 183      # 183 = ERROR_ALREADY_EXISTS
    if held:
        fresh = (time.time() - _last_beat()) < LOCK_FRESH_SEC
        if fresh and _pid_alive(_beat_pid()):
            return False                  # 确有活实例在运行
    _beat()                               # 接管：抢占心跳所有权
    return True


def main():
    if not _acquire_single_instance():
        # 已有实例在后台监测：提示后退出，避免多实例争抢 session.json / 重复托盘图标。
        # v18.13：原写法是静态模态框，无人点确定就会永远挂着（用户熄屏时尤其容易发生），
        # 表现为「进程在、但不采样不落盘」。改为 8 秒后自动关闭并退出。
        app = QApplication.instance() or QApplication(sys.argv)
        box = QMessageBox(QMessageBox.Icon.Information, "已在运行",
                          "PC 用电电费计算器 已在后台监测中。\n"
                          "请查看任务栏右下角托盘图标。\n\n"
                          "（本提示 8 秒后自动关闭）",
                          QMessageBox.StandardButton.Ok)
        box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        QTimer.singleShot(8000, box.close)
        box.exec()
        sys.exit(0)
    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 10))
    # v18.8：迷你悬浮窗是独立顶层窗口；退出统一走托盘「退出」，避免关窗误退
    app.setQuitOnLastWindowClosed(False)
    w = MainWindow()
    if getattr(w, "tray", None) is not None:
        # v18 后台常驻：启动即入托盘，不占任务栏；监测已在后台自动开始
        w.hide()
        try:
            w.tray.showMessage("PC用电监测已启动", "后台监测运行中 · 双击托盘图标打开窗口",
                               QSystemTrayIcon.MessageIcon.Information, 3000)
        except Exception:
            pass
    else:
        w.show()
    # 首次刷新明细表
    w._refresh_breakdown()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
