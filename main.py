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
import ctypes
import shutil
import traceback
from ctypes import wintypes
from datetime import datetime, timedelta
from collections import deque
import logging

# v18.29+ W3：降级账本用的模块日志。未显式配置时由 logging 的 lastResort 处理打印到 stderr，
# 保证静默异常至少有迹可循（不再「零信号」）。
#
# 静默异常三段式约定（对应审查报告 §1.2 W3）：
#   ① 硬件探测类 —— 不允许静默：记日志 + 状态栏标注「部分硬件未识别」（见 _mark_degraded）。
#   ② UI 刷新类   —— 允许静默，但必须就近加注释「已知：UI 刷新失败可忽略，不影响主流程」。
#   ③ 落盘类     —— 不允许静默：必须留痕并给用户可见信号（见 _save_session / _load_session_maybe）。
_log = logging.getLogger("pc_power_calc")

from PySide6.QtCore import (Qt, QThread, QTimer, Signal, QElapsedTimer, QDateTime,
                            QEvent, QMarginsF, QSizeF)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QLineEdit, QDoubleSpinBox, QSpinBox,
    QDialog, QFormLayout, QMessageBox, QFileDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QSizePolicy, QSpacerItem, QCheckBox, QSystemTrayIcon, QMenu,
    QProgressBar, QInputDialog, QTextBrowser, QComboBox, QScrollArea,
    QGraphicsDropShadowEffect,
)
from PySide6.QtCharts import (QChart, QChartView, QLineSeries, QValueAxis,
                              QBarSeries, QBarSet, QBarCategoryAxis, QDateTimeAxis)
from PySide6.QtGui import (QPainter, QFont, QColor, QAction, QPixmap, QIcon,
                           QTextDocument, QBrush)
# v18.26 报告导出 PNG：沿用 QTextDocument 引擎直接画到 QImage，纯 Qt、完全离线，
# 相比 QWebEngine（+100MB）或 reportlab（第三方）成本最低，也不依赖 QtPrintSupport。

import hardware as H
import power_model as PM
import power_core as PC

DEFAULT_RATE = 0.56          # 元 / 千瓦时（居民电价参考，可在设置中修改）
APP_VERSION = "v18.38"       # 界面标题/托盘提示展示的版本号
WINDOW_HOURS = 24.0
SAMPLE_MS = 1000   # v18.32 默认采样/刷新间隔 1 秒（原 2000）。仍可在设置/曲线详情里改
# v18.13 常见电源额定功率档位：给「按推荐填入」取最接近的档，避免填出 543W 这种不存在的规格
PSU_COMMON = (300, 350, 400, 450, 500, 550, 600, 650, 700, 750, 800, 850, 1000)
# v18.11 手动标记「显示器已关」后，若检测到键鼠空闲短于该值，
# 说明人已回来，自动恢复按开屏计费，避免忘记切回导致长期低估
DISP_WAKE_IDLE_SEC = 20.0
# v18.14 手动「记为关闭」的最短生效期（秒）。在此之前即使有键鼠操作也不自动唤醒，
# 否则刚设完就被覆盖，用户根本看不到关屏后的功率下降。
DISP_MANUAL_GRACE_SEC = 300.0
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

# v18.29+ W5：设置字段单一真相源。新增设置只需在此加一行，
# _save_session / _load_session_maybe 自动同步，不再「改两处手写」。
# 仅收录「配置类」字段；运行期累计（energy_wh / hourly / samples 等）不在此列。
_SETTINGS_SPEC = [
    ("rate", DEFAULT_RATE), ("window_hours", WINDOW_HOURS), ("psu_eff", PM.PSU_EFFICIENCY),
    ("psu_rating_w", 0.0), ("calib_k", 1.0), ("calib_idle", 0.0), ("calib_peak", 0.0),
    ("price_mode", "单一"), ("rate_valley", 0.30), ("rate_flat", 0.56), ("rate_peak", 0.85),
    ("tier_base", 0.0), ("tier_l1", 2160.0), ("tier_r1", 0.56),
    ("tier_l2", 4800.0), ("tier_r2", 0.61), ("tier_r3", 0.86),
    ("autostart_monitor", False), ("tou_valley", (23, 7)), ("tou_peak", [(8, 11), (18, 21)]),
    ("alert_enabled", False), ("alert_threshold", 300.0),
    ("budget_kwh", 0.0), ("budget_cost", 0.0),
    ("budget_alert_enabled", False), ("budget_alert_pct", 90.0),
    ("idle_cpu_thresh", 5.0), ("idle_gpu_thresh", 15.0),
    ("idle_nudge_enabled", False), ("idle_nudge_min", 10.0),
]

# v18.28 真实显示器电源状态：注册 GUID_MONITOR_POWER_ON 通知，捕获 WM_POWERBROADCAST
# 事件。之前仅靠「空闲时长 ≥ 系统熄屏超时」推断显示器是否熄灭，手动按显示器电源键关屏
# （不走过系统超时）永远识别不到；本事件能直接拿到显示器开关的真值。
_MONITOR_POWER_ON_GUID = "{0273105A-6A1B-4244-AD7A-3A0B30C60E5D}"
_WM_POWERBROADCAST = 0x0218
_PBT_POWERSETTINGCHANGE = 0x8013
_DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_uint8 * 8)]


class _POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [("PowerSetting", _GUID),
                ("DataLength", ctypes.c_uint32),
                ("Data", ctypes.c_uint32)]


def _parse_guid_str(s: str):
    """'0273105A-6A1B-4244-AD7A-3A0B30C60E5D' -> (Data1, Data2, Data3, [8 字节])。"""
    h = s.strip().strip("{}").replace("-", "")
    d1 = int(h[0:8], 16)
    d2 = int(h[8:12], 16)
    d3 = int(h[12:16], 16)
    b = [int(h[i:i + 2], 16) for i in range(16, 32, 2)]
    return d1, d2, d3, b


def _guid_to_str(g) -> str:
    return "{%08X-%04X-%04X-%02X%02X-%02X%02X%02X%02X%02X%02X}" % (
        g.Data1, g.Data2, g.Data3,
        g.Data4[0], g.Data4[1], g.Data4[2], g.Data4[3],
        g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7])


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
QLabel#big { font-size: 28px; font-weight: 700; color: #1f2a44; }
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


def _cfg_compact(hw) -> list:
    """v18.19 悬浮窗右上角配置信息：压缩成短型号，224px 宽度内一眼可读。

    例：'12th Gen Intel(R) Core(TM) i5-12400' → 'i5-12400'；
        'NVIDIA GeForce RTX 3060' → 'GPU RTX 3060'。
    """
    import re
    out = []
    cpu = (getattr(hw, "cpu_name", "") or "").replace(
        "(R)", "").replace("(TM)", "").replace("®", "").replace("™", "")
    m = (re.search(r"\bi[3579]-?\d{3,5}[A-Z]{0,3}\b", cpu)
         or re.search(r"Ryzen\s*\d\s*\w*", cpu, re.I)
         or re.search(r"\bA\d-\d{4}\b", cpu))
    out.append(m.group(0) if m else cpu.strip()[:16])
    gpu = (getattr(hw, "gpu_name", "") or "")
    m = (re.search(r"(RTX|GTX|RX)\s*\d{3,4}\s*\w*", gpu, re.I)
         or re.search(r"Radeon\s+\w+", gpu, re.I)
         or re.search(r"Arc\s+A\d+", gpu, re.I))
    out.append(("GPU " + m.group(0)) if m else (gpu.strip()[:16] if gpu else ""))
    if getattr(hw, "ram_bytes", 0):
        out.append("内存 %.0fG" % (hw.ram_bytes / 1e9))
    mc = getattr(hw, "monitor_count", 0)
    if mc:
        out.append("%d 屏" % mc)
    return [x for x in out if x]


class MiniOverlay(QWidget):
    """v18.15 桌面迷你悬浮窗：贴着桌面显示、背景全透明、可拖动、右键切层级。

    两种层级（右键悬浮窗切换，选择会记进会话）：
      · 嵌入桌面（默认）：WindowStaysOnBottomHint —— 在壁纸之上、所有普通窗口
        之下，视觉上就是「长在桌面上」，不遮挡任何程序，Win+D 也能看到。
        代价：任何最大化窗口都会盖住它，这是桌面层的固有行为。
      · 始终置顶：WindowStaysOnTopHint —— 任何窗口之上都可见。

    v18.15 关键修正：不再用 SetParent 把窗口挂进桌面。
      v18.13 把窗口 SetParent 到 Progman / SHELLDLL_DefView，窗口身份就从
      「顶层窗口」变成了「子窗口」；而背景透明依赖 WS_EX_LAYERED，
      分层子窗口在 Windows 上不渲染 —— 结果就是悬浮窗彻底隐身（用户反馈
      「桌面上不显示」）。改为保持顶层窗口 + 置底标志，观感一致但不碰层级。

    背景全透明：不画底板只显示文字，靠黑色描边保证深浅壁纸上都看得清。
    v18.13 起取消双击隐藏（v18.8 行为），避免拖动时误触把窗口弄丢。
    """
    def __init__(self, on_top: bool = False, show_bd: bool = True):
        super().__init__(None)
        self._on_top = bool(on_top)
        self._show_bd = bool(show_bd)
        self._last_cfg = ""
        self.setWindowFlags(self._flags_for(self._on_top))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(6)

        # 顶部：左=实时功率/状态/电费，右=配置信息（v18.19 配置放右上角）
        top = QHBoxLayout()
        top.setSpacing(14)
        live = QVBoxLayout()
        live.setSpacing(1)
        self.lbl_w = QLabel("— W")
        self.lbl_w.setStyleSheet("color:#eef3ff; font-size:26px; font-weight:800;")
        self.lbl_sub = QLabel("监测中")
        self.lbl_sub.setStyleSheet("color:#9fb4d8; font-size:11px;")
        live.addWidget(self.lbl_w)
        live.addWidget(self.lbl_sub)
        top.addLayout(live)
        top.addStretch(1)
        cfg = QVBoxLayout()
        cfg.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        cfg.setSpacing(1)
        self.lbl_cfg = QLabel("")
        self.lbl_cfg.setStyleSheet("color:#9fb4d8; font-size:10px;")
        self.lbl_cfg.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        cfg.addWidget(self.lbl_cfg)
        top.addLayout(cfg)
        root.addLayout(top)

        # 电费行独占整行（v18.21 修复：原来挤在顶行左侧，被右侧配置列
        # 压缩后「¥金额」被裁掉；独占一行 + 11px + 2 位小数保证 196px 内完整）
        self.lbl_cost = QLabel("")
        self.lbl_cost.setStyleSheet("color:#ffd28a; font-size:11px; font-weight:600;")
        root.addWidget(self.lbl_cost)

        # 温度行（v18.24：CPU/GPU/硬盘实时温度，右对齐小字；无数据自动收起）
        self.lbl_temp = QLabel("")
        self.lbl_temp.setStyleSheet("color:#9fb4d8; font-size:9px;")
        self.lbl_temp.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        root.addWidget(self.lbl_temp)

        # 底部：完整功耗结构（v18.22 票据式两列小表——名称右对齐 + 瓦数右对齐
        # 固定列，行由 set_breakdown 动态重建；末尾金色「合计」行）
        self.bd_widget = QWidget()
        self.bd_widget.setStyleSheet("background:transparent;")
        bd = QVBoxLayout(self.bd_widget)
        bd.setContentsMargins(0, 0, 0, 0)
        bd.setSpacing(1)
        bd.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self.bd_rows = QVBoxLayout()
        self.bd_rows.setContentsMargins(0, 0, 0, 0)
        self.bd_rows.setSpacing(1)
        bd.addLayout(self.bd_rows)
        root.addWidget(self.bd_widget)
        self._bd_row_widgets = []     # 当前行控件（重建时销毁，含末尾合计行）

        # 没有底板，给文字加黑色描边（offset=0 的阴影即形成轮廓），
        # 否则浅色壁纸上白色文字会完全看不见。
        for _l in (self.lbl_w, self.lbl_sub, self.lbl_cost, self.lbl_cfg, self.lbl_temp):
            _sh = QGraphicsDropShadowEffect(_l)
            _sh.setBlurRadius(8)
            _sh.setOffset(0, 0)
            _sh.setColor(QColor(0, 0, 0, 235))
            _l.setGraphicsEffect(_sh)
        self.setToolTip("左键拖动可移动\n右键：切换「嵌入桌面 / 始终置顶」或隐藏")
        self._drag = None
        self._layer_cb = None      # 层级切换回调（主窗口用来写会话）
        self._hide_cb = None       # 右键「隐藏」回调
        self._bd_cb = None         # 功耗构成显示开关回调
        # 尺寸随「是否显示功耗构成」变化，必须放在控件建好之后
        self.bd_widget.setVisible(self._show_bd)
        self._apply_size()

    # ---------------- 功耗构成 ----------------
    def _apply_size(self):
        """v18.22 宽度固定 240，高度随内容自动伸缩。

        顶部：实时功率/状态（左）+ 配置信息（右）；
        中部：电费整行；
        底部：功耗结构票据式小表（每行名称+瓦数）+ 合计行。
        高度按各块实际像素累加。
        """
        self.setFixedWidth(320)                  # v18.36: 240→320，构成行四列（+使用率/温度）
        h = 20                                   # 上下边距 10+10
        # 顶部块（左：功率/状态；右：配置）+ 电费整行 + 功耗构成
        live_h = 0
        for _l in (self.lbl_w, self.lbl_sub):
            live_h += _l.sizeHint().height() + 1
        cfg_h = self.lbl_cfg.sizeHint().height() if self.lbl_cfg.text() else 0
        h += max(live_h, cfg_h) + 6             # 顶部块 + 与下方的间距
        h += self.lbl_cost.sizeHint().height() + 1
        if self.lbl_temp.text():
            h += self.lbl_temp.sizeHint().height() + 1
        if self._show_bd and self._bd_row_widgets:
            for _w in self._bd_row_widgets:
                h += _w.sizeHint().height() + 1
        self.setFixedHeight(max(120, h))
        # v18.23 内容变高后（票据式表格比旧文本高），历史位置可能把窗口
        # 底部顶出屏幕——每次尺寸变化后钳制回屏内
        self._clamp_into_screen()

    def _clamp_into_screen(self):
        """v18.23 把悬浮窗钳制回所在屏幕的可用区域（不压任务栏）。"""
        try:
            scr = self.screen() or QApplication.primaryScreen()
            if scr is None:
                return
            g = scr.availableGeometry()
            x = min(max(self.x(), g.left()), g.right() - self.width())
            y = min(max(self.y(), g.top()), g.bottom() - self.height())
            if (x, y) != (self.x(), self.y()):
                self.move(x, y)
        except Exception:
            pass

    def bd_visible(self) -> bool:
        return bool(self._show_bd)

    def set_bd_visible(self, on: bool, notify: bool = True):
        """开关功耗构成小表（右键菜单 / 会话恢复共用）。"""
        self._show_bd = bool(on)
        self.bd_widget.setVisible(self._show_bd)
        self._apply_size()
        if notify and callable(self._bd_cb):
            try:
                self._bd_cb(self._show_bd)
            except Exception:
                pass

    def _bd_row(self, name: str, watts: float, total: bool = False,
                util_text: str = None, temp_text: str = None,
                temp_tip: str = None):
        """v18.36 四列票据行：部件 | 使用率 | 功耗 W | 温度/转速。

        温度列带 °（如 79°）= 温度、纯数字（如 1948）= 风扇转速。
        total=True 时只有部件名与功耗（金色），中间两列留空。"""
        row = QWidget()
        row.setStyleSheet("background:transparent;")
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        def _lbl(text, color, w=None, bold=False):
            lb = QLabel(text)
            lb.setStyleSheet("color:%s; font-size:10px;%s" % (
                color, " font-weight:700;" if bold else ""))
            lb.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if w:
                lb.setFixedWidth(w)
            # 行内文字同样加黑描边（无底板，浅色壁纸可见）
            _sh = QGraphicsDropShadowEffect(lb)
            _sh.setBlurRadius(8)
            _sh.setOffset(0, 0)
            _sh.setColor(QColor(0, 0, 0, 235))
            lb.setGraphicsEffect(_sh)
            return lb

        c_name = "#ffd28a" if total else "#b9cbe8"
        c_val = "#ffd28a" if total else "#eef3ff"
        lay.addWidget(_lbl(name, c_name, bold=total), 1)
        lay.addWidget(_lbl(util_text if util_text else "", "#9fb4d8", 34))
        lay.addWidget(_lbl("%.0f W" % watts, c_val, 46, bold=total))
        _tl = _lbl(temp_text if temp_text else "", "#9fb4d8", 48)
        if temp_tip:
            _tl.setToolTip(temp_tip)
        lay.addWidget(_tl)
        return row

    def set_breakdown(self, bd: dict, util: dict = None, temps: dict = None,
                      temps_tip: dict = None):
        """v18.22 票据式功耗结构，v18.36 扩展四列（+使用率、温度/转速）。
        util/temps：{部件名: 显示文本}；temps_tip：温度列的 LHM 参考读数提示。"""
        util = util or {}
        temps = temps or {}
        temps_tip = temps_tip or {}
        try:
            items = sorted(((str(k), float(v)) for k, v in (bd or {}).items()),
                           key=lambda kv: -kv[1])
            items = [(k, v) for k, v in items if v > 0.05]
        except Exception:
            items = []
        # 清空旧行
        while self._bd_row_widgets:
            _w = self._bd_row_widgets.pop()
            self.bd_rows.removeWidget(_w)
            _w.deleteLater()
        if items:
            for k, v in items:
                _row = self._bd_row(k, v, util_text=util.get(k),
                                    temp_text=temps.get(k),
                                    temp_tip=temps_tip.get(k))
                self.bd_rows.addWidget(_row)
                self._bd_row_widgets.append(_row)
            _row = self._bd_row("合计", sum(v for _, v in items), total=True)
            self.bd_rows.addWidget(_row)
            self._bd_row_widgets.append(_row)
        self._apply_size()

    def set_config(self, cfg_lines):
        """v18.19 右上角配置信息（多行小字，右对齐）。空内容自动收起。"""
        try:
            txt = "\n".join(str(x) for x in (cfg_lines or []) if str(x).strip())
        except Exception:
            txt = ""
        self.lbl_cfg.setText(txt)
        self._apply_size()

    def place_right(self):
        """v18.19 默认定位：整个悬浮窗落在屏幕右侧 15% 区域内（水平居中于该区、垂直居中）。

        只在「无历史拖动位置」时作为初始位置使用；用户拖动后由会话记忆接管。
        """
        try:
            scr = self.screen() or QApplication.primaryScreen()
            g = scr.availableGeometry()
            band = max(int(g.width() * 0.15), self.width() + 20)
            x = g.right() - band + (band - self.width()) // 2
            y = g.top() + (g.height() - self.height()) // 2
            self.move(x, y)
        except Exception:
            pass

    # ---------------- 层级模式 ----------------
    @staticmethod
    def _flags_for(on_top: bool):
        f = (Qt.WindowType.FramelessWindowHint |
             Qt.WindowType.Tool |
             Qt.WindowType.WindowDoesNotAcceptFocus)
        # 置底 / 置顶二选一：置底 = 贴在桌面上且不遮挡任何窗口
        f |= (Qt.WindowType.WindowStaysOnTopHint if on_top
              else Qt.WindowType.WindowStaysOnBottomHint)
        return f

    def layer_on_top(self) -> bool:
        """True=始终置顶，False=嵌入桌面（置底）。"""
        return bool(self._on_top)

    def set_layer(self, on_top: bool, notify: bool = True):
        """切换「嵌入桌面 / 始终置顶」。改窗口标志后必须 hide+show 才生效。"""
        on_top = bool(on_top)
        self._on_top = on_top
        was = self.isVisible()
        self.hide()
        self.setWindowFlags(self._flags_for(on_top))
        if was:
            self.show()
        if notify and callable(self._layer_cb):
            try:
                self._layer_cb(self._on_top)
            except Exception:
                pass

    # ---------------- 交互 ----------------
    def contextMenuEvent(self, ev):
        menu = QMenu(self)
        act_top = menu.addAction("始终置顶（盖在所有窗口之上）")
        act_top.setCheckable(True)
        act_top.setChecked(self._on_top)
        act_bot = menu.addAction("嵌入桌面（不遮挡任何窗口）")
        act_bot.setCheckable(True)
        act_bot.setChecked(not self._on_top)
        menu.addSeparator()
        act_bd = menu.addAction("显示功耗构成（全部项）")
        act_bd.setCheckable(True)
        act_bd.setChecked(self._show_bd)
        act_home = menu.addAction("恢复默认位置（屏幕右侧）")   # v18.20
        menu.addSeparator()
        act_hide = menu.addAction("隐藏悬浮窗")
        chosen = menu.exec(ev.globalPos())
        if chosen == act_top:
            self.set_layer(True)
        elif chosen == act_bot:
            self.set_layer(False)
        elif chosen == act_bd:
            self.set_bd_visible(not self._show_bd)
        elif chosen == act_home:
            # v18.20 一键回到默认位置（屏幕右侧 15% 区带、垂直居中）
            self.place_right()
        elif chosen == act_hide:
            if callable(self._hide_cb):
                try:
                    self._hide_cb()
                    return
                except Exception:
                    pass
            self.hide()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None


class ChartView(QChartView):
    """v18.32 主界面功耗曲线视图。

    双击行为分两种模式：
    · on_double_click 给了回调（主界面）：双击打开曲线详情大图；
    · 未给回调（详情大图内）：双击复位缩放（配合 rubber band 框选放大）。
    """

    def __init__(self, chart, on_double_click=None, parent=None):
        super().__init__(chart, parent)
        self._on_dbl = on_double_click

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if callable(self._on_dbl):
                self._on_dbl()
                return
            self.chart().zoomReset()     # 详情模式：双击复位
            return
        super().mouseDoubleClickEvent(e)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"PC 电脑用电电费计算器 {APP_VERSION}")
        self.resize(1080, 920)  # v18.35 +40px：实时卡片（含硬件信息块）sizeHint 需要
        self.setStyleSheet(CSS)

        # v18.29+ W3：降级账本（收敛静默异常，给维护者/用户可见信号）
        self._degraded = set()
        self._degraded_notes = {}

        # ---- 状态 ----
        # 硬件检测失败绝不能让整个程序崩溃：降级到空模板并登记账本
        try:
            self.hw = H.detect_hardware()
        except Exception:
            _log.exception("硬件检测失败，使用空模板继续运行")
            self.hw = H.HardwareInfo()
            self._degraded.add("hardware")
            self._degraded_notes["hardware"] = "硬件检测失败·已用空模板(估算偏差大)"
        self.model = PM.build_model(self.hw)
        self.calib_k = 1.0
        self.calib_idle = 0.0
        self.calib_peak = 0.0
        self.model.calib_k = self.calib_k
        self.model.calib_idle = self.calib_idle
        self.model.calib_peak = self.calib_peak
        # v18.29+ W4：把 ~80 行属性默认值抽到 _init_defaults，__init__ 只保留骨架
        self._init_defaults()

        self._build_ui()
        # v18 系统信息侧栏静态数据（一次性 WMI/注册表采集）
        try:
            self._sys_static = H.collect_system_info()
        except Exception as _e:
            self._sys_static = {}
        self._refresh_header_static()   # v18.33 标题后的操作系统文本
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
        self.mini = MiniOverlay(on_top=getattr(self, '_mini_on_top', False),
                            show_bd=getattr(self, '_mini_bd', True))
        self.mini.hide()
        if self._mini_visible:
            if self._mini_pos:
                self.mini.move(int(self._mini_pos[0]), int(self._mini_pos[1]))
            else:
                # v18.19 无历史位置：默认整个落在屏幕右侧 15% 区域
                self.mini.place_right()
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
    def _init_defaults(self):
        """v18.29+ W4：从 __init__ 抽出的 ~80 行属性默认值，集中维护、便于阅读。"""
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
        # v18.15 悬浮窗层级：True=始终置顶，False=嵌入桌面（置底）
        self._mini_on_top = False
        # v18.17 悬浮窗是否显示功耗构成行
        self._mini_bd = True
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
        # v18.28 真实显示器电源状态（来自 GUID_MONITOR_POWER_ON 事件）：True=显示器已物理关闭
        self._monitor_phys_off = False
        self._monitor_hook_handle = None

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # v18.34 最左侧系统信息侧栏已删除，硬件信息整体并入实时卡片右侧
        page = QVBoxLayout()
        page.setContentsMargins(18, 16, 18, 16)
        page.setSpacing(14)
        outer.addLayout(page, 1)
        # v18.9：设置面板改为抽屉式浮层——不挤占布局宽度，打开时浮在内容上方右侧
        self._settings_dock = QScrollArea(root)
        self._settings_dock.setWidgetResizable(True)
        self._settings_dock.setFrameShape(QFrame.Shape.NoFrame)
        self._settings_dock.setFixedWidth(520)
        # v18.13 横向滚动条不能关：45 行表单的最小宽度会超过抽屉宽，
        # 关掉之后超出的部分直接被裁掉（实测「设置显示不全」就是这个原因）
        self._settings_dock.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
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
        # v18.33 运行时间/操作系统从左侧栏上移到标题后面（左侧栏只留硬件主题）
        self._hdr_uptime = QLabel("运行 —")
        self._hdr_uptime.setStyleSheet("color:#5a6478;font-size:12px;")
        self._hdr_os = QLabel("")
        self._hdr_os.setStyleSheet("color:#5a6478;font-size:12px;")
        sub = QLabel(APP_VERSION)
        sub.setToolTip("实时监测 · 24 小时汇总")
        sub.setStyleSheet("color:#8a93a6;font-size:12px;")
        # QLabel 的 minimumSizeHint=整段文本宽度，会把主界面顶宽（v18.17 教训）；
        # 显式 setMinimumWidth(1) 覆盖之（setMinimumWidth(0) 等于未设置，无效）。
        # 注意不能用 Ignored 策略：布局会把 Ignored 项宽度按 0 分配，整段文字消失
        # （v18.32 的副标题其实就是这样被压没了，v18.33 一并修正）。
        for _lbl in (self._hdr_uptime, self._hdr_os, sub):
            _lbl.setMinimumWidth(1)
        top.addWidget(t)
        top.addSpacing(12)
        top.addWidget(self._hdr_uptime)
        top.addSpacing(12)
        top.addWidget(self._hdr_os)
        top.addItem(QSpacerItem(20, 10, QSizePolicy.Expanding))
        top.addWidget(sub)
        page.addLayout(top)
        # v18.33 静态数据在 _build_ui 之后采集（__init__），此处先占位、
        # 由 _refresh_header_static() 在采集完成后回填操作系统文本
        self._refresh_header_static()

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
        # v18.17 外层竖排：7 个指标一行 + 详解行独占一整行。
        # 之前详解行（"直流 X W + 电源损耗 Y W = 插座 Z W（效率…）"）夹在第一列里，
        # 它是最长的文本，直接把这张卡片的最小宽度顶到 1572px，
        # 主界面被连带撑到 1983px（resize(1080) 形同虚设）。挪出来就好了。
        outer = QVBoxLayout(c); outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(8)
        # v18.34 横排：左侧读数网格 + 右侧硬件信息块（原最左侧栏整体并入，
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
        # 插座功耗（大）
        col1 = QVBoxLayout(); col1.setSpacing(2)
        col1.addWidget(self._lbl("插座实时功耗", "title"))
        self.wall_big = self._lbl("0.0 W", "big")
        col1.addWidget(self.wall_big)

        # 累计电量
        col2 = QVBoxLayout(); col2.setSpacing(2)
        col2.addWidget(self._lbl("累计电量", "title"))
        self.energy_big = self._lbl("0.000 kWh", "big")
        col2.addWidget(self.energy_big)
        self.cost_sub = self._lbl("电费 ¥0.00", "sub")
        col2.addWidget(self.cost_sub)

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

        # CPU/GPU 负载
        col4 = QVBoxLayout(); col4.setSpacing(2)
        col4.addWidget(self._lbl("实时负载", "title"))
        self.cpu_lbl = self._lbl("CPU 0%", "hw"); self.cpu_lbl.setStyleSheet("font-size:16px;font-weight:600;color:#2b3552;")
        self.gpu_lbl = self._lbl("GPU —", "hw"); self.gpu_lbl.setStyleSheet("font-size:16px;font-weight:600;color:#2b3552;")
        col4.addWidget(self.cpu_lbl); col4.addWidget(self.gpu_lbl)
        self.psu_lbl = self._lbl("", "sub")
        col4.addWidget(self.psu_lbl)

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

        # v18.32 主界面移除「月度预算」卡片（用户要求）：预算功能本身保留——
        # 设置面板可设、达到阈值仍弹预警通知、报告内仍有预算进度。

        # 待机占比
        col7 = QVBoxLayout(); col7.setSpacing(2)
        col7.addWidget(self._lbl("待机占比", "title"))
        self.idle_big = self._lbl("—", "big")
        col7.addWidget(self.idle_big)
        self.idle_sub = self._lbl("空闲时段耗电", "sub")
        col7.addWidget(self.idle_sub)

        # 详解行独占整行：最长的一句，给它完整宽度，避免撑宽上面 7 列。
        # v18.35 行尾并入「重新检测硬件」按钮 + 检测状态（原在硬件信息块顶部，
        # 挪出后信息块可独占卡片右侧全高）。
        self.wall_sub = self._lbl(
            "插座 0.0 W ＝ 直流 0.0 + 损耗 0.0 · 效率 0.85", "sub")
        self.wall_sub.setWordWrap(True)
        self.wall_sub.setMinimumWidth(0)
        btn_row = QHBoxLayout(); btn_row.setSpacing(10)
        btn_rd = QPushButton("⟳ 重新检测硬件")
        btn_rd.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_rd.setToolTip("插拔硬盘 / 显示器、更换硬件后点此重建功耗模型。\n"
                          "静态功耗与部件清单会随之变化，电源负载率与转换效率也跟着重算。\n"
                          "（校准数据与电源额定功率设置会保留）")
        btn_rd.setStyleSheet(
            "QPushButton{background:#f4f6fa;border:1px solid #d6dbe6;border-radius:5px;"
            "padding:3px 10px;font-size:12px;color:#2f3b52;}"
            "QPushButton:hover{background:#e8eefb;border-color:#9db4e8;}")
        btn_rd.clicked.connect(self._redetect_hardware)
        self._hw_detect_lbl = QLabel("启动时已检测")
        self._hw_detect_lbl.setStyleSheet("font-size:11px;color:#8a94a6;")
        # v18.35 状态标签从控制条挪到详解行行尾（控制条整行让给 9 个平铺按钮）
        self.status_lbl = QLabel("就绪")
        self.status_lbl.setStyleSheet("color:#6b7488;font-size:12px;")
        self.status_lbl.setMinimumWidth(1)
        btn_row.addWidget(self.wall_sub, 1)
        btn_row.addWidget(self.status_lbl)
        btn_row.addWidget(btn_rd)
        btn_row.addWidget(self._hw_detect_lbl)
        outer.addLayout(btn_row)

        for _i, _col in enumerate((col1, col2, col3, col4, col5, col7)):
            grid.addLayout(_col, _i // 3, _i % 3)

        # v18.17 QLabel 默认「最小宽度 = 整段文本宽度」。填上真实数据后
        # （如 "1,234.5 W"、"23:59:59"）各列会互相顶宽，卡片最小宽度从
        # 空态 792px 涨到 1232px，主界面又被撑开。
        # v18.35 不能用 Ignored 策略：Ignored 项 sizeHint 记 0，QGridLayout
        # 按 sizeHint 分配后其余列全被压成 0 宽——6 个读数列只剩 1 列显示、
        # 右侧大片空白（用户截图反馈的根因）。改 setMinimumWidth(1)：
        # 显式覆盖 minimumSizeHint（0 = 未设置无效），列按内容铺开、窄窗口
        # 下仍可压缩不顶宽（与 v18.33 标题行同款修法）。
        for _lb in c.findChildren(QLabel):
            if _lb is getattr(self, "_hw_detect_lbl", None):
                continue
            _lb.setMinimumWidth(1)

        return c

    def _chart_card(self) -> QWidget:
        c = QWidget(); self._card(c)
        lay = QVBoxLayout(c); lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        h = QLabel("功耗曲线（近 60 分钟 · 插座功耗 W）· 双击查看详情")
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
        # v18.32 双击曲线 -> 打开大图详情（open_chart_detail）
        self.chart_view = ChartView(self.chart, on_double_click=self.open_chart_detail)
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        # v18.17 QChartView 自带较大的最小尺寸，是「中间一行」顶宽主界面的主因
        self.chart_view.setMinimumWidth(0)
        self.chart_view.setMinimumHeight(120)
        lay.addWidget(self.chart_view, 1)
        return c

    def _breakdown_card(self) -> QWidget:
        c = QWidget(); self._card(c)
        lay = QVBoxLayout(c); lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        h = QLabel("功耗构成（估算）")
        h.setStyleSheet("font-size:13px;color:#2b3552;font-weight:600;")
        lay.addWidget(h)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["部件", "使用率", "功耗 W", "温度 / 转速"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # v18.36 纯展示表禁用选择：首行默认 currentRow 的蓝色 selection
        # 会透过进度条单元格的透明容器渗出（EXE 冒烟实测 CPU 行出现蓝色大块）
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # v18.37 双击「温度/转速」列 → 打开 LibreHardwareMonitor 传感器详情
        self.table.cellDoubleClicked.connect(self._on_table_dbl)
        lay.addWidget(self.table, 1)
        # PSU 建议
        self.psu_hint = QLabel("")
        self.psu_hint.setStyleSheet("font-size:12px;color:#6b7488;")
        lay.addWidget(self.psu_hint)
        # v18.17 同实时卡片：表格/提示的最小宽度不再反向顶宽主界面
        self.table.setMinimumWidth(0)
        for _lb in c.findChildren(QLabel):
            _lb.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            _lb.setMinimumWidth(0)
        return c

    # ---------------- 系统信息块（v18.34 并入实时卡片右侧） ----------------
    def _refresh_header_static(self):
        """v18.33 标题后的操作系统文本（静态，采集/重检测后调用）。"""
        lbl = getattr(self, "_hdr_os", None)
        if lbl is None:
            return
        # v18.34 _build_ui 里标题行先于 _live_card(内含 _sysinfo_panel)构建，
        # _sys_static 可能尚未初始化，须防御。
        s = getattr(self, "_sys_static", None) or {}
        os_txt = self._short_model(s.get("os"), 30)
        arch = (s.get("osarch") or "").strip()
        lbl.setText(" · ".join(x for x in (os_txt, arch) if x and x != "—"))

    def _uptime_line(self) -> str:
        """v18.33 运行时间行（标题后与侧栏共用）：开机时长 + 当前日期时间。"""
        dyn = self._sys_dyn or {}
        boot = dyn.get("boot")
        if not boot:
            return "运行 —"
        up = max(0, time.time() - boot)
        hh, rem = divmod(int(up), 3600)
        mm, _ss = divmod(rem, 60)
        now = datetime.now()
        wk = "一二三四五六日"[now.weekday()]
        return (f"运行 {hh}时{mm:02d}分 · {now:%m-%d} [{wk}] {now:%H:%M}")

    def _sysinfo_panel(self) -> QWidget:
        """v18.35 改为内嵌块：不再是最左侧固定宽侧栏，而是实时卡片右半区。

        「重新检测硬件」按钮已挪到卡片底部详解行行尾（v18.35），
        本块只剩硬件信息富文本，独占卡片右侧全高。
        成员名 sysinfo_view / _hw_detect_lbl 保持不变，_update_sysinfo /
        _redetect_hardware 无需改动。
        """
        self.sysinfo_view = QTextBrowser()
        self.sysinfo_view.setStyleSheet(
            "QTextBrowser{background:#f7f9fc;color:#1a1a1a;"
            "border:1px solid #eef1f6;border-radius:8px;"
            "font-family:'Consolas','Microsoft YaHei';font-size:11px;"
            "padding:8px 10px;}")
        self.sysinfo_view.setOpenExternalLinks(False)
        self.sysinfo_view.setFrameShape(QFrame.Shape.NoFrame)
        # v18.35 双列后内容全高 ~200px：设最小高度让实时卡片自动加高，
        # 从下方功耗曲线区挪 ~30px，信息区完整显示不出滚动条
        self.sysinfo_view.setMinimumHeight(220)
        self._sys_dyn = {}
        self._sys_static = {}
        return self.sysinfo_view

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

    @staticmethod
    def _short_model(name, maxlen: int = 26) -> str:
        """v18.32 窄侧栏显示用的硬件型号精简：去噪音后缀、去料号、超长截断。

        例：'AMD Ryzen 5 5600X 6-Core Processor' -> 'AMD Ryzen 5 5600X'；
            'ST1000LM035-1RK174' -> 'ST1000LM035'（- 后多为固件/料号）。
        """
        import re as _re
        n = (str(name or "")).strip()
        n = _re.sub(r"\s*@.*$", "", n)                          # @ 3.70GHz
        n = _re.sub(r"\s+\d+\s*[-~]?\s*[cC]ore\s+Processor\s*$", "", n)  # 6-Core Processor
        n = n.replace(" Processor", "").replace(" CPU", "")
        n = _re.sub(r"^Microsoft\s+", "", n)                    # Microsoft Windows -> Windows
        n = _re.sub(r"\s+Family\s+Controller\s*$", "", n)       # Realtek ... Family Controller
        if len(n) > maxlen:
            head = n.split("-", 1)[0]                           # 盘型号取 '-' 前段
            n = head if len(head) >= 6 and len(head) <= maxlen else (n[:maxlen - 1] + "…")
        return n or "—"

    def _build_sysinfo_html(self) -> str:
        # v18.32 简化版：288px 窄栏下旧版一行塞 3 个字段必然折行错乱。
        # 原则：一行一主题、子行只缩进 12px、砍低价值字段（TPM/域/BIOS 版本/
        # Cache 容量/插槽数/MAC/网关/DPI），型号统一经 _short_model 精简。
        s = self._sys_static or {}
        dyn = self._sys_dyn or {}
        g = s.get
        ram_total = dyn.get("ram_total") or self.hw.ram_bytes or 0
        ram_used = dyn.get("ram_used") or 0
        ram_free = ram_total - ram_used
        ram_pct = dyn.get("ram_pct") or 0.0

        def gb(b):
            return f"{b / (1 << 30):.2f}GB" if b else "—"

        # v18.36 CPU 温度兜底：主板热区（多数台式机为空）→ LHM Web Server；
        # 主板温度同样来自 LHM（SuperIO），没装 LHM 时两处都显示 —。
        st = dyn.get("sensor_temps") or {}
        cpu_t = dyn.get("cpu_temp") or st.get("cpu")
        board_t = st.get("motherboard")
        gpu_t = dyn.get("gpu_temp")
        mhz = dyn.get("mhz") or (s.get("mhz") or 0)
        vram = s.get("gpuvram")
        vram_txt = f"{vram / (1 << 30):.0f}GB" if vram else "—"
        Y = "<span style='color:#111111;font-weight:bold'>"   # v18.5 白底黑字：标签黑色加粗
        E = "</span>"
        SUB = "margin-left:12px;color:#555555;"
        # v18.35 双列布局：硬件信息从 288px 窄栏变成实时卡片右半区（~400px 宽、
        # 高度有限），单列 17 行放不下会出滚动条。拆成左右两列（table 布局，
        # QTextDocument 唯一可靠的分栏方案），高度减半、不再滚动。
        LA = []   # 左列：主板/处理器/内存/网络
        LB = []   # 右列：显卡/磁盘
        # v18.35 「启动模式」按用户要求删除（UEFI/Legacy + SecureBoot 属低频
        # 信息，占一行空间不值）；v18.33 起运行时间/操作系统也已在顶部标题后
        LA.append(f"<div style='margin:2px 0 3px 0;'>{Y}主　板{E} "
                  f"{self._short_model(g('mb'), 18)} {self._temp_html(board_t, 55, 70)}</div>")
        cpu_name = (g('cpu') or self.hw.cpu_name or "—").strip()
        cpu_badge = (" <span style='color:#b8860b;font-weight:bold;'>⚠ 未识别</span>"
                     ) if getattr(self.model, "cpu_conf", "high") == "low" else ""
        LA.append(f"<div style='margin-bottom:1px;'>{Y}处理器{E} "
                  f"{self._short_model(cpu_name, 18)}{cpu_badge}</div>")
        cpu_load = self.cur.get("cpu_load") if isinstance(self.cur, dict) else None
        cpu_load = 0.0 if cpu_load is None else cpu_load
        # v18.36 按用户要求移除处理器/物理内存的 ASCII 使用率条：
        # 使用率已由下方「功耗构成」表的进度条列统一呈现，此处只保留规格+温度。
        LA.append(f"<div style='{SUB}margin-bottom:3px;'>{g('cores') or '—'}核"
                  f"{g('threads') or '—'}线程 · {mhz / 1000.0:.2f}GHz · "
                  f"{self._temp_html(cpu_t, 75, 85)}</div>")
        LA.append(f"<div style='margin-bottom:1px;'>{Y}物理内存{E} {gb(ram_total)}</div>")
        LA.append(f"<div style='{SUB}margin-bottom:3px;'>已用 {gb(ram_used)} · 可用 {gb(ram_free)}</div>")
        mods = (s.get("mods") or [])[:4]
        if mods:
            _parts = []
            for m in mods:
                mn = (m.get("m") or "").replace("Unknown", "GeIL").split()[0] if (m.get("m") or "") else "—"
                cap = int(m.get("cap") or 0) / (1 << 30)
                _parts.append(f"{mn} {cap:.0f}G")
            LA.append(f"<div style='{SUB}margin-bottom:3px;'>内存条 {' · '.join(_parts)}"
                      f" DDR4/{mods[0].get('clk') or mods[0].get('spd') or '—'}</div>")
        # v18.35 网络（含 IP/速率）紧跟物理内存/内存条之后（用户指定位置），
        # 左列顺序：启动模式 → 主板 → 处理器 → 内存 → 网络；右列：显卡 → 磁盘。
        nic = g('nic')
        if nic:
            dn = dyn.get("down_kbs"); upk = dyn.get("up_kbs")
            speed = (f" ↓{dn:.1f}K ↑{upk:.1f}K"
                     if dn is not None and upk is not None else "")
            LA.append(f"<div style='margin:4px 0 1px;'>{Y}网络{E} {self._short_model(nic, 14)}</div>")
            LA.append(f"<div style='{SUB}margin-bottom:3px;'>IP {g('ip') or '—'}{speed}</div>")
        gpu_badge = (" <span style='color:#b8860b;font-weight:bold;'>⚠ 未识别</span>"
                     ) if getattr(self.model, "gpu_conf", "high") == "low" else ""
        LB.append(f"<div style='margin:2px 0 1px;'>{Y}显卡{E} "
                  f"{self._short_model(g('gpuname') or self.hw.gpu_name, 18)} · {vram_txt} · "
                  f"{self._temp_html(gpu_t, 65, 78)}{gpu_badge}</div>")
        LB.append(f"<div style='{SUB}margin-bottom:3px;'>{g('gpures') or '—'}\"　"
                  f"{g('gpuref') or '—'}Hz</div>")
        dtemps = dyn.get("disk_temps") or {}
        for i, dk in enumerate(g('disks') or []):
            media = (dk.get("media") or "").upper()
            tag = "SSD" if "SSD" in media else ("HDD" if "HDD" in media else (dk.get("bus") or ""))
            dt = dtemps.get(dk.get("model") or "")
            LB.append(f"<div style='margin-bottom:1px;'>{Y}磁盘{i}{E} "
                      f"{self._short_model(dk.get('model'), 14)} [{tag}] "
                      f"{dk.get('sizeGB') or '—'}GB {dk.get('letters') or ''} "
                      f"{self._temp_html(dt, 45, 55)}</div>")
        # v18.35 风扇转速（LibreHardwareMonitor/OpenHardwareMonitor WMI，装了才有）
        fans = dyn.get("fans") or []
        if fans:
            fan_txt = " · ".join(f"{n} {v}RPM" for n, v in fans[:3])
            LB.append(f"<div style='margin:4px 0 1px;'>{Y}风扇{E} {fan_txt}</div>")
        # table 分栏（HTML 属性 width/valign——QTextDocument 对嵌套 table 的
        # CSS 不可靠，v18.32 报告排版已验证过）
        return (f"<table width='100%' cellspacing='0' cellpadding='0'>"
                f"<tr><td width='50%' valign='top'>{''.join(LA)}</td>"
                f"<td width='50%' valign='top'>{''.join(LB)}</td></tr></table>")

    def _update_sysinfo(self):
        # v18.33 标题后的运行时间标签每拍都更新（QLabel.setText 开销极小），
        # 侧栏富文本仍按 v18.2 节流约 6s 刷一次。
        up_lbl = getattr(self, "_hdr_uptime", None)
        if up_lbl is not None:
            try:
                up_lbl.setText(self._uptime_line())
            except Exception:
                pass
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
            except Exception as e:
                # v18.29+ W3：系统静态信息采集失败不应崩，但需登记降级
                _log.warning("系统静态信息采集失败: %s", e)
                static = None
                self._mark_degraded("sysinfo", "系统信息采集失败·动态信息缺失")
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
            self._clear_degraded("sysinfo")
        self._refresh_header_static()   # v18.33 重检测后同步标题 OS 文本
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
        self.btn_export = QPushButton("导出报告(PNG)"); self.btn_export.setObjectName("ghost")
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
        self.btn_apps = QPushButton("软件耗电"); self.btn_apps.setObjectName("ghost")
        self.btn_apps.clicked.connect(self.open_apps)
        # v18.37 传感器详情（以 LibreHardwareMonitor 为参考的温度/转速对照）
        self.btn_sensors = QPushButton("传感器"); self.btn_sensors.setObjectName("ghost")
        self.btn_sensors.clicked.connect(self.open_sensors)
        # v18.35 平铺：9 个按钮一行均分铺满整张卡片（此前 5+4 两行第二行
        # 右侧是空的，用户截图要求把空白平铺掉）。卡片全宽 ~1012px，
        # 9 按钮 × ~105px + 间距刚好放下；列 stretch 均分，窄窗口按比例压缩。
        # 状态标签挪到实时卡片底部详解行行尾，控制条整行让给按钮。
        grid = QGridLayout(); grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        _btns = (self.btn_reset, self.btn_export, self.btn_csv, self.btn_compare,
                 self.btn_history, self.btn_sim, self.btn_hourly, self.btn_apps,
                 self.btn_sensors, self.btn_settings)
        for _i, _b in enumerate(_btns):
            grid.addWidget(_b, 0, _i)
            grid.setColumnStretch(_i, 1)
            _b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay.addLayout(grid, 1)
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
            # v18.14 关键修复：自动唤醒必须等「生效期」过了才允许发生。
            # 之前刚点完菜单（空闲≈0s）就立刻被唤醒覆盖，用户选了「记为关闭」
            # 却看不到数值下降，随后自己操作电脑负载上升 —— 看起来就是
            # 「关屏后功率反而变高」。生效期内即使有操作也保持关闭。
            try:
                _idle = H.user_idle_sec()
            except Exception:
                _idle = 1e9
            _held = time.time() - float(getattr(self, "_disp_manual_ts", 0.0) or 0.0)
            if _idle < DISP_WAKE_IDLE_SEC:
                if _held < DISP_MANUAL_GRACE_SEC:
                    return False          # 生效期内：有操作也保持「关闭」
                if not getattr(self, "_disp_wake_notified", False):
                    self._disp_wake_notified = True
                    try:
                        self._notify(
                            "已自动恢复按「显示器开启」计算",
                            f"「记为关闭」已生效 {DISP_MANUAL_GRACE_SEC/60:.0f} 分钟，"
                            f"且检测到键鼠操作（空闲 {_idle:.0f} 秒），"
                            f"为避免长期低估已自动恢复。\n\n"
                            f"需要再按关闭计，请在托盘菜单重新选择。")
                    except Exception:
                        pass
                return True
            self._disp_wake_notified = False
            return False
        # v18.28 真实显示器电源事件优先：手动按显示器电源键关屏也能识别，
        # 不再依赖「空闲时长 ≥ 系统熄屏超时」这一只能感知系统自动熄屏的启发式
        if getattr(self, "_monitor_phys_off", False):
            return False
        try:
            return not H.display_auto_off()
        except Exception:
            return True

    def _set_display_manual(self, state):
        """state: None=自动检测 / True=强制记为开 / False=强制记为关（熄屏省电）"""
        self._disp_manual = state
        self._disp_manual_ts = time.time()     # v18.14 记下手动标记时间，用于生效期判定
        self._disp_wake_notified = False       # 换档后重新允许提示一次
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

    # ---------------- v18.28 真实显示器电源事件 ----------------
    def _install_monitor_power_hook(self):
        """向主窗口注册 GUID_MONITOR_POWER_ON 通知，捕获显示器电源开关事件。

        仅 Windows 有效；非 Windows 或注册失败则静默跳过（退回空闲推断）。
        必须在窗口有原生句柄后调用（winId() 会强制创建）。
        """
        if sys.platform != "win32":
            return
        try:
            u = ctypes.windll.user32
            u.RegisterPowerSettingNotificationW.argtypes = (
                ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.c_uint32)
            u.RegisterPowerSettingNotificationW.restype = ctypes.c_void_p
            hwnd = int(self.winId())
            if not hwnd:
                return
            g = _GUID()
            d1, d2, d3, b = _parse_guid_str(_MONITOR_POWER_ON_GUID)
            g.Data1, g.Data2, g.Data3 = d1, d2, d3
            for i in range(8):
                g.Data4[i] = b[i]
            h = u.RegisterPowerSettingNotificationW(
                ctypes.c_void_p(hwnd), ctypes.byref(g), _DEVICE_NOTIFY_WINDOW_HANDLE)
            self._monitor_hook_handle = h or None
        except Exception:
            self._monitor_hook_handle = None

    def nativeEvent(self, eventType, message):
        """拦截 WM_POWERBROADCAST / PBT_POWERSETTINGCHANGE：显示器开关实时更新。"""
        if sys.platform == "win32" and eventType == b"windows_generic_MSG":
            try:
                msg = ctypes.cast(int(message), ctypes.POINTER(wintypes.MSG)).contents
                if msg.message == _WM_POWERBROADCAST and msg.wParam == _PBT_POWERSETTINGCHANGE:
                    pbs = ctypes.cast(msg.lParam,
                                     ctypes.POINTER(_POWERBROADCAST_SETTING)).contents
                    if _guid_to_str(pbs.PowerSetting) == _MONITOR_POWER_ON_GUID:
                        self._monitor_phys_off = (pbs.Data == 0)   # 0=关屏 1=开屏
                        self.display_on = self._display_on()
                        try:
                            self._refresh_readout()
                        except Exception:
                            pass
                        try:
                            self._save_session()
                        except Exception:
                            pass
                        try:
                            self._sync_disp_menu()
                        except Exception:
                            pass
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

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
            # v18.38 GPU 真实占用率（nvidia-smi utilization.gpu），供使用率列显示
            "gpu_util": data.get("gpu_util"),
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
        disp_on = bool(getattr(self, "display_on", True))
        _mon = float(getattr(self.model, "components", {}).get("显示器", 0.0) or 0.0)
        if not disp_on:
            disp_note = (f" · 显示器关 −{_mon:.0f}W"
                         f" · 累计省 {self._disp_saved_wh/1000.0:.3f} 度")
        elif _mon > 0:
            # v18.13 开屏也把状态写明：用户分不清「读到的数是开屏还是关屏」，
            # 只标注关屏的话，开屏时那行看不出显示器到底计没计进去。
            disp_note = f" · 显示器 +{_mon:.0f}W"
        # v18.11：填了额定功率就用 80 PLUS 曲线算出的实时效率，否则用固定效率
        _eff_live = self.cur.get("psu_eff")
        if not _eff_live:
            _eff_live = self.model.psu_efficiency
        _eff_note = " 动态" if float(getattr(self, "psu_rating_w", 0.0) or 0.0) > 0 else ""
        # v18.13 修正标签：self.cur['sys'] 是直流功耗（不含电源损耗），
        # 含损耗的是上面的大数字 wall。旧文案把这行标成「含电源损耗」，
        # 于是「大数字 220W / 这行 190W」看起来像是开关屏数值反了。
        # v18.32 精简文案：旧版「直流 X W + 电源损耗 Y W = 插座 Z W（效率 E（动态））」
        # 嵌套括号+整行过长不美观，改为单行短句、中点分隔。
        _loss = max(0.0, float(self.cur["wall"]) - float(self.cur["sys"]))
        self.wall_sub.setText(
            f"插座 {self.cur['wall']:.1f} W ＝ 直流 {self.cur['sys']:.1f} + 损耗 {_loss:.1f}"
            f" · 效率 {_eff_live:.2f}{_eff_note}{calib_note}{disp_note}")
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

        # 月度预算预警（v18.32 起主界面不再显示预算卡片，仅保留预警通知；
        # 预算在设置面板配置，进度见导出报告）
        if self.budget_kwh > 0 or self.budget_cost > 0:
            pct_k = (kwh_month / self.budget_kwh * 100.0) if self.budget_kwh > 0 else 0.0
            pct_c = (month_cost / self.budget_cost * 100.0) if self.budget_cost > 0 else 0.0
            pct = max(pct_k, pct_c)
            # 预警：达到设定比例且未已提醒则弹通知，低于则复位（单次越限只提示一次）
            if self.budget_alert_enabled and pct >= self.budget_alert_pct and not self._budget_alert_active:
                self._budget_alert_active = True
                self._notify("用电预算预警",
                             f"本月预估用电已达预算的 {pct:.0f}%（电费约 ¥{month_cost:,.0f} / 预算 ¥{self.budget_cost:,.0f}）")
            elif pct < self.budget_alert_pct:
                self._budget_alert_active = False
        else:
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

    # v18.35 构成表固定行顺序（用户指定）；不在表内的部件（待机/损耗等）
    # 按原顺序排在末尾
    _BD_ORDER = ("CPU", "GPU", "内存", "风扇", "SSD", "HDD",
                 "主板/芯片组", "显示器", "外设")

    def _sorted_bd_keys(self, keys):
        order = {n: i for i, n in enumerate(self._BD_ORDER)}
        unk = 0
        def _key(k):
            nonlocal unk
            if k in order:
                return (order[k], 0)
            unk += 1
            return (99, unk)
        return sorted(keys, key=_key)

    def _util_pct(self, name):
        """v18.35 使用率(0-100)：CPU=真实负载，GPU=估算功率/TDP，内存=内存占用；
        其余部件返回 None（进度条显示 —）。"""
        n = str(name).upper()
        if "CPU" in n:
            v = self.cur.get("cpu_load") if isinstance(self.cur, dict) else None
            return max(0.0, float(v)) if v is not None else None
        if "GPU" in n:
            # v18.38 真实占用率优先：nvidia-smi utilization.gpu（与任务管理器同口径）
            # → LHM 的 GPU Core Load（A 卡/无 nvidia-smi）→ 功耗÷TDP 兜底（标注估算）。
            u = self.cur.get("gpu_util") if isinstance(self.cur, dict) else None
            if u is not None:
                self._gpu_util_src = "nvidia-smi"
                return max(0.0, min(100.0, float(u)))
            try:
                u = H.lhm_gpu_load()
            except Exception:
                u = None
            if u is not None:
                self._gpu_util_src = "LHM"
                return u
            gp = self.cur.get("gpu_power") if isinstance(self.cur, dict) else None
            tdp = float(getattr(self.model, "gpu_tdp", 0) or 0)
            if gp is not None and tdp > 0:
                self._gpu_util_src = "估算"
                return min(100.0, float(gp) / tdp * 100)
            self._gpu_util_src = None
            return None
        if "内存" in str(name):
            v = (getattr(self, "_sys_dyn", None) or {}).get("ram_pct")
            return max(0.0, float(v)) if v else None
        return None

    @staticmethod
    def _make_util_cell() -> QWidget:
        """v18.36 使用率进度条：横向铺满单元格，高度 20px，
        百分比文字居中显示在条内（用户指定）。无数据时显示 —。"""
        w = QWidget()
        w.setStyleSheet("background:transparent;")
        w.setMinimumHeight(24)
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        pb = QProgressBar()
        pb.setRange(0, 100)
        pb.setTextVisible(True)
        pb.setFormat("—")
        pb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pb.setFixedHeight(20)
        pb.setStyleSheet(
            "QProgressBar{border:1px solid #d6dbe6;border-radius:4px;background:#f2f4f8;"
            "text-align:center;font-size:10px;color:#2b3552;}"
            "QProgressBar::chunk{background:#5aa832;border-radius:3px;margin:1px;}")
        h.addWidget(pb, 1)
        w._bar = pb
        w._lbl = None          # v18.36b 百分比移回条内居中，右侧标签取消
        return w

    def _update_util_bar(self, cell, name):
        """刷新使用率进度条：百分比居中 + 档位颜色（<70 绿 / <90 橙 / ≥90 红）。
        颜色样式只在跨档时重设，避免每 2s 全表 setStyleSheet。"""
        pb = cell._bar
        pct = self._util_pct(name)
        if pct is None:
            pb.setValue(0)
            pb.setFormat("—")
            return
        pb.setFormat("%p%")
        pb.setValue(int(pct + 0.5))
        col = "#5aa832" if pct < 70 else ("#d99a17" if pct < 90 else "#d8492f")
        if getattr(pb, "_chunk_col", None) != col:
            pb.setStyleSheet(
                "QProgressBar{border:1px solid #d6dbe6;border-radius:4px;background:#f2f4f8;"
                "text-align:center;font-size:10px;color:#2b3552;}"
                f"QProgressBar::chunk{{background:{col};border-radius:3px;margin:1px;}}")
            pb._chunk_col = col

    # v18.36 各部件温度告警/危险阈值（与 _temp_html 保持一致）
    _TEMP_LIMITS = {"cpu": (75, 85), "gpu": (65, 78), "disk": (45, 55),
                    "memory": (50, 60), "board": (55, 70)}

    def _temp_text_color(self, name):
        """v18.36 构成表「温度 / 转速」列。返回 (文本, QColor|None)。

        数据来源（按优先级）：
          CPU   — 主板热区计数器（Win32_PerfFormattedData...ThermalZone，多数
                  台式机为空）→ LibreHardwareMonitor WMI（sensor_temps.cpu）
          内存  — LHM WMI（sensor_temps.memory）；Windows 无免驱接口
          主板  — LHM WMI（sensor_temps.motherboard）；同上
          GPU   — nvidia-smi / ADL；磁盘 — StorageReliabilityCounter
          风扇  — 无温度概念，改显示最高转速 RPM（LHM WMI）
        没装 LHM 时 CPU/内存/主板/风扇显示 —（tooltip 会提示如何开启）。
        """
        n = str(name).upper()
        dyn = getattr(self, "_sys_dyn", None) or {}
        st = dyn.get("sensor_temps") or {}
        t = None
        warn, hot = 75, 85
        if "风扇" in str(name):
            # 风扇行显示转速而非温度：取所有风扇的最高转速
            fans = dyn.get("fans") or []
            if not fans:
                return "—", None
            rpm = max(int(v) for _n, v in fans)
            col = QColor("#1a1a1a") if rpm < 1500 else (
                QColor("#d99a17") if rpm < 2500 else QColor("#d8492f"))
            return f"{rpm} RPM", col
        if "CPU" in n:
            t = dyn.get("cpu_temp")
            if t is None:
                t = st.get("cpu")
            warn, hot = self._TEMP_LIMITS["cpu"]
        elif "GPU" in n:
            t = dyn.get("gpu_temp")
            if t is None:
                t = st.get("gpu")          # v18.37 回退 LHM（GPU Core）
            warn, hot = self._TEMP_LIMITS["gpu"]
        elif "内存" in str(name):
            t = st.get("memory")
            warn, hot = self._TEMP_LIMITS["memory"]
        elif "主板" in str(name) or "芯片" in str(name):
            t = st.get("motherboard")
            warn, hot = self._TEMP_LIMITS["board"]
        elif "SSD" in n or "HDD" in n:
            want = "SSD" if "SSD" in n else "HDD"
            temps = dyn.get("disk_temps") or {}
            for dk in ((getattr(self, "_sys_static", None) or {}).get("disks") or []):
                if want in (dk.get("media") or "").upper():
                    t = temps.get(dk.get("model") or "")
                    if t is not None:
                        break
            warn, hot = self._TEMP_LIMITS["disk"]
        if t is None:
            return "—", None
        col = QColor("#1a1a1a") if t < warn else (
            QColor("#d99a17") if t < hot else QColor("#d8492f"))
        return f"{t:.0f}°", col

    # v18.37 温度/转速列的「LHM 参考读数」：直接把 LibreHardwareMonitor 面板上
    # 的原始传感器名与读数列出来（带 Min/Max 之外的当前值），便于逐项核对。
    # Windows 免驱读不到 SuperIO，LHM 是唯一可信来源，故做显式对照。
    _LHM_PART_KIND = (("CPU", "cpu"), ("GPU", "gpu"), ("内存", "memory"),
                      ("主板", "motherboard"), ("芯片", "motherboard"),
                      ("风扇", "fan"), ("SSD", "disk"), ("HDD", "disk"))

    def _temp_tooltip(self, name):
        s = str(name)
        kind = None
        for kw, k in self._LHM_PART_KIND:
            if kw in s or kw in s.upper():
                kind = k
                break
        if kind is None:
            return ""
        try:
            rows = H.lhm_probe(kind)
        except Exception:
            return ""
        if not rows:
            # 用采样线程的 ready 判定（不在此处触发网络，避免卡 UI）：
            # 缓存尚未填充时 ready 也还没意义，先不提示，等首个采样周期落地。
            _ok = (getattr(self, "_sys_dyn", None) or {}).get("sensor_ready")
            if not _ok:
                return ("未连接到 LibreHardwareMonitor\n"
                        "温度/转速需经其 Web Server（127.0.0.1:8085）读取\n"
                        "（Windows 免驱读不到 SuperIO/EC 芯片）")
            if "内存" in s:
                return ("内存温度读不到：本机的内存条没有 SPD 温度探头\n"
                        "（LHM 只能读到容量与 SPD 时序；DDR5 / 部分高端 DDR4 才有温度探头）\n"
                        "这是硬件限制，HWiNFO、AIDA64 等同样读不到")
            return ("LibreHardwareMonitor 未报告「%s」的温度/转速\n"
                    "该部件本身通常不带温度探头（属正常现象）" % s)
        return ("LibreHardwareMonitor 参考读数\n"
                + "\n".join("• %s：%s" % (a, b) for a, b in rows[:14]))

    def open_sensors(self):
        """v18.37 传感器详情：照 LHM 的分组方式列出温度/风扇/控制等原始读数。"""
        TYPES = ("Temperature", "Fan", "Control", "Power", "Clock", "Load")
        TYPES_ALL = TYPES + ("Data", "Timing", "Voltage", "Current",
                             "Level", "Throughput", "Factor", "SmallData")
        d = QDialog(self)
        d.setWindowTitle("传感器详情 · LibreHardwareMonitor")
        d.resize(760, 580)
        vl = QVBoxLayout(d)
        tip = QLabel()
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#5a6478;font-size:12px;")
        vl.addWidget(tip)
        tree = QTableWidget(0, 4)
        tree.setHorizontalHeaderLabels(["传感器", "最小", "当前", "最大"])
        tree.verticalHeader().hide()
        tree.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tree.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        _hh = tree.horizontalHeader()
        _hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for _i in (1, 2, 3):
            _hh.setSectionResizeMode(_i, QHeaderView.ResizeToContents)
        vl.addWidget(tree, 1)

        _show_all = {"v": False}

        def _fill():
            try:
                snap = H.lhm_sensors(force=True)
            except Exception:
                snap = {"ok": False, "groups": []}
            tree.setRowCount(0)
            n = 0
            _ty = TYPES_ALL if _show_all["v"] else TYPES
            for g in snap.get("groups") or []:
                ss = [x for x in (g.get("sensors") or []) if x.get("type") in _ty]
                if not ss:
                    continue
                r = tree.rowCount()
                tree.insertRow(r)
                _p = str(g.get("parent") or "")
                _title = (f"{_p} › {g.get('name')}" if _p and _p != g.get("name")
                          else str(g.get("name") or ""))
                h = QTableWidgetItem(_title)
                f = h.font()
                f.setBold(True)
                h.setFont(f)
                h.setBackground(QBrush(QColor("#eef1f7")))
                tree.setItem(r, 0, h)
                tree.setSpan(r, 0, 1, 4)
                for x in ss:
                    r = tree.rowCount()
                    tree.insertRow(r)
                    _nm = str(x.get("name") or "")
                    _val = str(x.get("value") or "")
                    _bad = False
                    try:
                        _fv = float(_val.split()[0])
                        if x.get("type") == "Temperature":
                            _bad = not (5.0 <= _fv <= 100.0)
                        elif x.get("type") == "Fan":
                            _bad = _fv <= 0
                    except (ValueError, IndexError):
                        pass
                    _it = QTableWidgetItem("    " + _nm + ("（未接线）" if _bad else ""))
                    tree.setItem(r, 0, _it)
                    tree.setItem(r, 1, QTableWidgetItem(str(x.get("min") or "")))
                    _v = QTableWidgetItem(_val)
                    _vf = _v.font()
                    _vf.setBold(True)
                    _v.setFont(_vf)
                    tree.setItem(r, 2, _v)
                    tree.setItem(r, 3, QTableWidgetItem(str(x.get("max") or "")))
                    if _bad:   # LHM 面板对未接线的通道同样是灰色显示
                        _gray = QBrush(QColor("#9aa3b2"))
                        for _c in (0, 1, 2, 3):
                            tree.item(r, _c).setForeground(_gray)
                    n += 1
            if n:
                tip.setText("数据源：LibreHardwareMonitor Web Server（127.0.0.1:8085）。"
                            "分组、命名与「最小 / 当前 / 最大」三列与其界面一致。"
                            "灰色分组行 = 该硬件（LHM 中的节点名）。")
            else:
                tip.setText("未读取到 LibreHardwareMonitor 数据。请确认 "
                            "D:\\tools\\LibreHardwareMonitor\\LibreHardwareMonitor.exe 已运行，"
                            "并已开启 Web Server（选项 → Web Server → 端口 8085、Run）。")

        _fill()
        bar = QHBoxLayout()
        ck_all = QCheckBox("显示全部传感器（含内存 SPD 时序/容量）")
        ck_all.toggled.connect(lambda v: (_show_all.__setitem__("v", v), _fill()))
        bar.addWidget(ck_all)
        bar.addStretch(1)
        btn_ref = QPushButton("刷新")
        btn_ref.setObjectName("ghost")
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("ghost")
        btn_ref.clicked.connect(_fill)
        btn_close.clicked.connect(d.accept)
        bar.addStretch(1)
        bar.addWidget(btn_ref)
        bar.addWidget(btn_close)
        vl.addLayout(bar)
        d.exec()

    def _on_table_dbl(self, r, c):
        """双击构成表「温度/转速」列 → 打开 LHM 传感器详情对照。"""
        if c == 3:
            self.open_sensors()

    def _util_tip(self, name):
        """v18.38 使用率列的来源说明：GPU 行标注占用率是实测还是功耗估算。"""
        n = str(name).upper()
        if "GPU" in n:
            src = getattr(self, "_gpu_util_src", None)
            if src == "nvidia-smi":
                return "GPU 使用率：nvidia-smi 实测 SM 占用（与任务管理器同口径）"
            if src == "LHM":
                return "GPU 使用率：LibreHardwareMonitor 的 GPU Core 占用"
            if src == "估算":
                return ("GPU 使用率：由「功耗 ÷ TDP」估算\n"
                        "未取到实测占用（nvidia-smi 不可用且非 LHM 可识别显卡）")
        return ""

    def _refresh_breakdown(self):
        bd = self.cur.get("breakdown", {})
        keys = self._sorted_bd_keys(list(bd.keys()))
        # v18.27 差异更新：部件集合不变时只刷新数值单元格，避免每 2s 全表重建
        if getattr(self, "_bd_keys", None) == keys and getattr(self, "_bd_val_items", None):
            for name, item, pb, ti in zip(keys, self._bd_val_items,
                                          self._bd_util_bars, self._bd_temp_items):
                item.setText(f"{bd[name]:.1f}")
                self._update_util_bar(pb, name)
                pb.setToolTip(self._util_tip(name))
                txt, col = self._temp_text_color(name)
                ti.setText(txt)
                ti.setForeground(col if col is not None else self._bd_temp_gray)
                ti.setToolTip(self._temp_tooltip(name))
            return
        self.table.setRowCount(0)
        self._bd_val_items = []
        self._bd_util_bars = []
        self._bd_temp_items = []
        self._bd_temp_gray = QBrush(QColor("#8a94a6"))
        for name in keys:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(name)))
            cell = self._make_util_cell()
            self._update_util_bar(cell, name)
            cell.setToolTip(self._util_tip(name))
            self.table.setCellWidget(r, 1, cell)
            vi = QTableWidgetItem(f"{bd[name]:.1f}")
            self.table.setItem(r, 2, vi)
            txt, col = self._temp_text_color(name)
            ti = QTableWidgetItem(txt)
            ti.setForeground(col if col is not None else self._bd_temp_gray)
            ti.setToolTip(self._temp_tooltip(name))
            self.table.setItem(r, 3, ti)
            self._bd_val_items.append(vi)
            self._bd_util_bars.append(cell)
            self._bd_temp_items.append(ti)
        self._bd_keys = keys

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
        vl = QVBoxLayout(panel); vl.setContentsMargins(12, 12, 12, 12); vl.setSpacing(10)
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
        # v18.13 让输入框跟随可用宽度伸展，而不是把整行撑到超过抽屉宽度
        fl.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
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
        fl.addRow(QLabel("<b>计费方式</b>"))
        fl.addRow("  方式", mode)
        fl.addRow(QLabel("<b>阶梯电价（居民月用量分档）</b>"))
        fl.addRow("  本月已用基数", tbase)
        fl.addRow("  档1 上限", tl1)
        fl.addRow("  档1 电价", tr1)
        fl.addRow("  档2 上限", tl2)
        fl.addRow("  档2 电价", tr2)
        fl.addRow("  档3 电价", tr3)
        fl.addRow(QLabel("<b>峰谷分时</b>"))
        fl.addRow("  谷价", rv)
        fl.addRow("  平价", rf)
        fl.addRow("  峰价", rp)
        # 峰谷时段边界（可自定义）
        vsb = QSpinBox(); vsb.setRange(0, 23); vsb.setValue(self.tou_valley[0])
        veb = QSpinBox(); veb.setRange(0, 23); veb.setValue(self.tou_valley[1])
        p1s = QSpinBox(); p1s.setRange(0, 23); p1s.setValue(self.tou_peak[0][0] if len(self.tou_peak) > 0 else 0)
        p1e = QSpinBox(); p1e.setRange(0, 23); p1e.setValue(self.tou_peak[0][1] if len(self.tou_peak) > 0 else 0)
        p2s = QSpinBox(); p2s.setRange(0, 23); p2s.setValue(self.tou_peak[1][0] if len(self.tou_peak) > 1 else 0)
        p2e = QSpinBox(); p2e.setRange(0, 23); p2e.setValue(self.tou_peak[1][1] if len(self.tou_peak) > 1 else 0)
        fl.addRow(QLabel("<b>峰谷时段（小时，可改）</b>"))
        fl.addRow("  谷 起", vsb); fl.addRow("  谷 止", veb)
        fl.addRow("  峰1 起", p1s); fl.addRow("  峰1 止", p1e)
        fl.addRow("  峰2 起", p2s); fl.addRow("  峰2 止", p2e)
        fl.addRow(QLabel("  （平价=其余时段；起=止表示禁用该段）"))
        # v18.29+ W4：把告警/预算/待机/自启/显示器等高级设置拆到独立方法，降低本方法体量
        adv = self._build_advanced_settings_rows(fl)
        fl.addRow(QLabel("<b>精度校准</b>"))
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
                    "ck": ck, "cidle": cidle, "cpeak": cpeak}
        self._sw.update(adv)
        return panel

    def _build_advanced_settings_rows(self, fl: QFormLayout) -> dict:
        """v18.29+ W4：从 _build_settings_panel 抽出的高级设置分组（告警/预算/待机/自启/显示器）。

        直接把行加进传入的 fl，并返回控件名->控件的映射，供主方法并入 self._sw。
        """
        # 功耗告警
        al = QCheckBox("功耗超阈值告警（弹系统通知）")
        al.setChecked(self.alert_enabled)
        alth = QDoubleSpinBox(); alth.setRange(50, 2000); alth.setDecimals(0)
        alth.setValue(self.alert_threshold); alth.setSuffix(" W")
        fl.addRow(QLabel("<b>功耗告警</b>"))
        fl.addRow("  启用", al)
        fl.addRow("  告警阈值", alth)
        # 月度用电预算
        bk = QDoubleSpinBox(); bk.setRange(0, 100000); bk.setDecimals(0)
        bk.setValue(self.budget_kwh); bk.setSuffix(" kWh")
        bc = QDoubleSpinBox(); bc.setRange(0, 100000); bc.setDecimals(0)
        bc.setValue(self.budget_cost); bc.setSuffix(" ¥")
        bap = QDoubleSpinBox(); bap.setRange(1, 200); bap.setDecimals(0)
        bap.setValue(self.budget_alert_pct); bap.setSuffix(" %")
        bal = QCheckBox("预算达到阈值时弹系统通知")
        bal.setChecked(self.budget_alert_enabled)
        fl.addRow(QLabel("<b>月度用电预算</b>"))
        fl.addRow("  电量预算 kWh", bk)
        fl.addRow("  电费预算 ¥", bc)
        fl.addRow("  预警比例", bap)
        fl.addRow("  启用预算预警", bal)
        # 待机识别
        ict = QDoubleSpinBox(); ict.setRange(0, 50); ict.setDecimals(0)
        ict.setValue(self.idle_cpu_thresh); ict.setSuffix(" %")
        igt = QDoubleSpinBox(); igt.setRange(0, 200); igt.setDecimals(0)
        igt.setValue(self.idle_gpu_thresh); igt.setSuffix(" W")
        fl.addRow(QLabel("<b>待机识别</b>"))
        fl.addRow("  CPU 负载≤", ict)
        fl.addRow("  GPU 功耗≤", igt)
        # 待机自动提醒
        inud = QCheckBox("长时间空闲自动提醒（建议睡眠/关机）")
        inud.setChecked(self.idle_nudge_enabled)
        inm = QDoubleSpinBox(); inm.setRange(1, 120); inm.setDecimals(0)
        inm.setValue(self.idle_nudge_min); inm.setSuffix(" 分钟")
        fl.addRow(QLabel("<b>待机自动提醒</b>"))
        fl.addRow("  启用提醒", inud)
        fl.addRow("  连续空闲 ≥", inm)
        # 开机自启
        au = QCheckBox("开机自启（启动后最小化到托盘）")
        au.setChecked(self.autostart)
        aum = QCheckBox("开机自启后自动开始监测")
        aum.setChecked(self.autostart_monitor)
        fl.addRow(QLabel("<b>开机自启</b>"))
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
        return {"al": al, "alth": alth, "bk": bk, "bc": bc, "bap": bap, "bal": bal,
                "ict": ict, "igt": igt, "inud": inud, "inm": inm,
                "au": au, "aum": aum, "mini": mini_ck, "disp": disp_cb}

    def _save_settings_panel(self):
        s = self._sw
        rate, win, eff, samp, mode = s["rate"], s["win"], s["eff"], s["samp"], s["mode"]
        tbase, tl1, tr1, tl2, tr2, tr3 = s["tbase"], s["tl1"], s["tr1"], s["tl2"], s["tr2"], s["tr3"]
        rv, rf, rp = s["rv"], s["rf"], s["rp"]
        vsb, veb, p1s, p1e, p2s, p2e = s["vsb"], s["veb"], s["p1s"], s["p1e"], s["p2s"], s["p2e"]
        al, alth, bk, bc, bap, bal = s["al"], s["alth"], s["bk"], s["bc"], s["bap"], s["bal"]
        ict, igt, inud, inm = s["ict"], s["igt"], s["inud"], s["inm"]
        au, aum, ck, cidle, cpeak = s["au"], s["aum"], s["ck"], s["cidle"], s["cpeak"]
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
                # v18.26 自动归档也直接落 PNG 图片（此前是 PDF）
                out = os.path.join(BASE_DIR, f"用电报告_{datetime.now().strftime('%Y%m%d_%H%M')}.png")
                if not self._render_png(html, out):
                    out = ""
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
        # v18.26 主按钮改为 PNG（图片，可直接贴图/分享）；HTML 保留作为备用（可二次排版/贴进文档）
        row = QHBoxLayout()
        bt = QPushButton("保存报告(PNG)"); bt.setObjectName("primary")
        bt.clicked.connect(lambda: self._save_png(html))
        bt2 = QPushButton("保存报告(HTML)"); bt2.setObjectName("ghost")
        bt2.clicked.connect(lambda: self._save_html(html))
        bt3 = QPushButton("关闭"); bt3.setObjectName("ghost")
        bt3.clicked.connect(d.accept)
        row.addWidget(bt); row.addWidget(bt2); row.addStretch(1); row.addWidget(bt3)
        vl.addLayout(row)
        d.exec()

    def _build_report_html(self) -> str:
        kwh = self.energy_wh / 1000.0
        cost = self._current_cost()
        # v18.29+ W3：报告内展示降级项，让用户知道哪些数据可能不准
        deg = self._degraded_summary()
        degraded_block = (
            f"<div class='card' style='border-left:4px solid #b8860b;'>"
            f"<div class='k' style='color:#b8860b;'>⚠ 部分功能已降级运行</div>"
            f"<div style='font-size:12px;'>{deg}</div>"
            f"<div style='font-size:11px;color:#8a93a6;margin-top:2px;'>"
            f"上述相关数据可能不准确，建议检查硬件/权限或点「重新检测硬件」。</div></div>"
        ) if deg else ""
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
        # v18.15 迷你悬浮窗快照：把悬浮窗此刻显示的三行内容原样写进报告，
        # 这样导出的 PDF 里也能看到「导出瞬间」的实时读数，而不只是汇总值。
        mini_block = ""
        try:
            _m = getattr(self, "mini", None)
            _wall = float(self.cur.get("wall", 0.0) or 0.0)
            _sys = float(self.cur.get("sys", 0.0) or 0.0)
            _eff = float(self.cur.get("psu_eff") or self.psu_eff or 0.0)
            _eff_note = "（动态）" if float(getattr(self, "psu_rating_w", 0.0) or 0.0) > 0 else ""
            _disp = "开" if self.cur.get("display_on", True) else "关"
            _mon = float(getattr(self.model, "components", {}).get("显示器", 0.0) or 0.0)
            _dyn = "动态" if _eff_note else "固定"
            _mw = _m.lbl_w.text() if _m is not None else f"{_wall:.0f} W"
            _ms = _m.lbl_sub.text() if _m is not None else ("监测中" if self.running else "已暂停")
            _mc = _m.lbl_cost.text() if _m is not None else ""
            if not _mc:
                _mc = "本轮 %.3f kWh · ¥%.2f" % (self.energy_wh / 1000.0, self._current_cost())
            _pos = f"（{_m.x()}, {_m.y()}）" if _m is not None else "—"
            _vis = "显示中" if (_m is not None and _m.isVisible()) else "已隐藏"
            _dock = ("始终置顶" if (_m is not None and _m.layer_on_top())
                     else "嵌入桌面（置底）")
            _mbd = ""
            if _m is not None and _m.bd_visible():
                _bd = (self.cur or {}).get("breakdown") or {}
                _its = sorted(((str(_k), float(_v)) for _k, _v in _bd.items()),
                              key=lambda _kv: -_kv[1])
                _its = [(k, v) for k, v in _its if v > 0.05]
                if _its:
                    _top, _rest = _its[:4], _its[4:]
                    _ps = ["%s %.0f" % (k, v) for k, v in _top]
                    if _rest:
                        _ps.append("其他 %.0f" % sum(v for _, v in _rest))
                    _mbd = " · ".join(_ps) + " W"
            mini_block = (
                f"<div class='card'><div class='k'>迷你悬浮窗（导出瞬间快照）</div>"
                f"<div style='font-size:12px;line-height:1.7;margin-top:3px;'>"
                f"瞬时插座功耗：{_mw}<br>"
                f"状态：{_ms} · 显示器{_disp} · {_dock} · {_vis}<br>"
                f"本轮累计：{_mc}<br>"
                f"插座/直流：{_wall:.1f} W / {_sys:.1f} W · 效率 {_eff:.2f}{_dyn} · "
                f"显示器 {_mon:.0f} W（已省 {self._disp_saved_wh/1000.0:.3f} kWh）<br>"
                f"构成：{_mbd or '—'}<br>"
                f"窗口位置：{_pos}"
                f"</div></div>")
        except Exception:
            mini_block = ""
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
        bars = self._report_hourly_bars()
        bd = self.cur.get("breakdown", {})
        bd_rows = "".join(f"<tr><td>{k}</td><td style='text-align:right'>{v:.1f} W</td></tr>" for k, v in bd.items())
        # v18.32 横屏版式：QTextDocument **不支持 flex / column-count**（实测 4 个 flex
        # 子项 x 全为 4、y 递增，即被当作块级竖排），因此横向分栏只能用 table 实现。
        # 结构：标题行 → 4 格 KPI 一行 → 降级提示 → 主体三栏 → 双列柱图 → 脚注。
        return f"""
<html><head><meta charset="utf-8"><style>
body{{font-family:'Microsoft YaHei',sans-serif;background:#f4f6f9;color:#1f2a44;margin:0;padding:10px;}}
.h{{font-size:18px;font-weight:700;}}
.card{{background:#fff;border-radius:9px;padding:8px 10px;margin:0 0 7px 0;border:1px solid #e6e9ef;}}
.box{{background:#fff;border-radius:9px;padding:7px 10px;border:1px solid #e6e9ef;}}
.k{{color:#8a93a6;font-size:11px;}} .v{{font-size:21px;font-weight:700;}}
table{{width:100%;border-collapse:collapse;font-size:12px;}}
td{{padding:2px 4px;border-bottom:1px solid #eef1f7;}}
.nt{{border:0;}} .nt td{{border:0;vertical-align:top;}}
</style></head><body>
<table class="nt"><tr>
  <td style="width:70%;"><div class="h">PC 用电电费 · 24 小时汇总</div></td>
  <td style="width:30%;text-align:right;"><div class="k">生成时间：{now}</div></td>
</tr></table>
<table class="nt"><tr>
  <td style="width:25%;padding-right:3px;"><div class="box"><div class="k">累计电量</div>
    <div class="v">{kwh:.3f} kWh</div></div></td>
  <td style="width:25%;padding:0 3px;"><div class="box"><div class="k">电费（{self.rate:.2f} 元/度）</div>
    <div class="v">¥{cost:.2f}</div></div></td>
  <td style="width:25%;padding:0 3px;"><div class="box"><div class="k">平均插座功耗</div>
    <div class="v">{avg_w:.0f} W</div></div></td>
  <td style="width:25%;padding-left:3px;"><div class="box"><div class="k">峰值插座功耗</div>
    <div class="v">{self.peak_wall:.0f} W</div></div></td>
</tr></table>
<div style="height:7px;"></div>
{degraded_block}
<table class="nt"><tr>
  <td style="width:34%;padding-right:4px;">
    <div class="card"><div class="k">本机配置</div>
      <div style="font-size:12px;line-height:1.65;margin-top:3px;">
      CPU：{self.hw.cpu_name}（{self.hw.cpu_cores}C/{self.hw.cpu_threads}T）<br>
      GPU：{self.hw.gpu_name}{(' · 真实功耗' if self.hw.gpu_is_nvidia else ' · 估算')}<br>
      内存：{self.hw.ram_bytes/1e9:.1f} GB · 存储：{', '.join(f'{t} {s:.0f}G' for t,s in self.hw.disks)}<br>
      显示器：{self.hw.monitor_count} 台 · 系统：{self.hw.os_caption}
      </div></div>
    <div class="card"><div class="k">功耗构成（当前估算）</div>
      <table>{bd_rows}</table></div>
    {pblock}
  </td>
  <td style="width:33%;padding:0 4px;">
    {proj}
    {idle_block}
    {apps_block}
  </td>
  <td style="width:33%;padding-left:4px;">
    <div class="card"><div class="k">逐小时平均功耗</div>{bars}</div>
    {mini_block}
  </td>
</tr></table>
<div class="k" style="margin-top:4px;">注：台式机无墙插电表，GPU（N 卡）采用 nvidia-smi 真实读数，
CPU 与其余部件按负载/经验模型估算，结果仅供参考。{calib_note}</div>
</body></html>"""

    def _report_hourly_bars(self) -> str:
        """v18.29+ W4：从 _build_report_html 抽出的逐小时平均功耗柱图 HTML。

        v18.32：改为**双列**排布。QTextDocument 不支持 flex/多栏，24 行柱图单列会
        让报告高度翻倍成为竖长条；拆成两列后高度减半，配合三栏主体可得到横屏比例。
        """
        keys = sorted(self.hourly.keys())
        if not keys:
            return ""
        vals = [self.hourly[k][0] / self.hourly[k][1] for k in keys if self.hourly[k][1]]
        maxv = max(vals) if vals else 1.0     # 全 0 采样时避免 max() 空序列抛错

        def _bar(k):
            s, n = self.hourly[k]
            avg = s / n if n else 0.0
            pct = (avg / maxv * 100) if maxv else 0.0
            # v18.32：柱条用 HTML 属性 width+bgcolor 的嵌套 table 实现。
            # 实测 QTextDocument 里三层写法只有这层可靠——CSS width/background
            # 在嵌套 table 中不生效（条形不渲染），HTML 属性则正常。
            bar_row = ("<table width='100%' border='0' cellspacing='0' cellpadding='0'><tr>"
                       f"<td width='{pct:.0f}%' bgcolor='#2f6bff' "
                       "style='font-size:7px;padding:0;'>&nbsp;</td>"
                       "<td bgcolor='#eef1f7' style='font-size:7px;padding:0;'>&nbsp;</td>"
                       "</tr></table>")
            return (f"<div style='margin:1px 0;'>"
                    f"<div style='font-size:9px;color:#555;'>{k} · {avg:.0f}W</div>{bar_row}</div>")

        half = (len(keys) + 1) // 2
        left = "".join(_bar(k) for k in keys[:half])
        right = "".join(_bar(k) for k in keys[half:])
        return (f"<table class='nt'><tr>"
                f"<td style='width:50%;padding-right:6px;'>{left}</td>"
                f"<td style='width:50%;padding-left:6px;'>{right}</td></tr></table>")

    # v18.32 横屏版式参数（笔记本/显示器都是横屏，报告图片也应为横版）
    REPORT_WIDTH = 1600        # 逻辑宽度起点（px）
    REPORT_WIDTH_MIN = 1100    # 收窄下限（内容很少时别压太窄）
    REPORT_WIDTH_MAX = 2400    # 加宽上限（再宽字就太小了）
    REPORT_RATIO = 1.70        # 目标 宽/高，接近 16:9(1.78)，留一点余量
    REPORT_OUT_PX = 3000       # 输出图片宽度上限（像素），scale 据此反推

    def _render_png(self, html: str, path: str, width: int = 0, scale: float = 0.0) -> bool:
        """把报告 HTML 渲染成 PNG 图片；成功返回 True，不弹任何对话框。

        沿用 QTextDocument 引擎（与旧版 _render_pdf 同一套 HTML 解析），只是输出
        目标从 QPrinter(PdfFormat) 换成 QImage——纯 Qt，完全离线，也不让 exe 膨胀。

        v18.32 横屏自适应
        -----------------
        旧版固定 width=960，而报告内部用 display:flex 分栏——但 **QTextDocument 不支持
        flex**（实测 4 个 flex 子项的 x 全为 4、y 逐行递增，即退化成块级竖排），
        结果产出 960×1476 的竖长条（0.65:1），横屏上要一路滚动才能看完。
        本版改为：① 报告 HTML 用 table 分栏（QTextDocument 唯一可靠的横排方案）；
        ② 逻辑宽度不再固定，而是迭代搜索到让 宽/高 ≈ REPORT_RATIO 的值——
        加宽则换行变少、高度下降，单调收敛，7 次内必停；
        ③ scale 由输出宽度上限反推，保证不同内容量下图片像素宽度都在 3000 左右。

        width / scale 传 0（默认）即走自适应；显式传值则按调用方指定渲染。
        """
        try:
            from PySide6.QtGui import QPainter, QImage, QColor
            doc = QTextDocument()
            doc.setDefaultFont(QFont("Microsoft YaHei", 10))
            doc.setHtml(html)

            w = int(width) if width else self.REPORT_WIDTH
            for _ in range(7):
                doc.setTextWidth(w)
                hh = float(doc.size().height())
                if hh <= 0:
                    break
                ratio = w / hh
                if self.REPORT_RATIO * 0.95 <= ratio <= self.REPORT_RATIO * 1.05:
                    break
                # 太竖 -> 加宽（换行变少、高度下降）；太扁 -> 收窄（高度上升）
                step = int(w * 0.10) if ratio < self.REPORT_RATIO else -int(w * 0.10)
                nw = max(self.REPORT_WIDTH_MIN, min(self.REPORT_WIDTH_MAX, w + step))
                if nw == w:      # 已到边界，停止
                    break
                w = nw

            doc.setTextWidth(w)
            size = doc.size()
            w, h = int(size.width()), int(size.height())
            if w <= 0 or h <= 0:
                return False
            s = float(scale) if scale else min(2.0, self.REPORT_OUT_PX / float(w))
            if s <= 0:
                s = 2.0
            img = QImage(int(w * s), int(h * s), QImage.Format_ARGB32)
            img.fill(QColor("#f4f6f9"))      # 与报告 body 底色一致
            painter = QPainter(img)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            painter.scale(s, s)
            doc.drawContents(painter)
            painter.end()
            return img.save(path, "PNG")
        except Exception as e:
            try:
                print("[png] 渲染失败: %r" % (e,))
            except Exception:
                pass
            return False

    def _save_html(self, html: str):
        path, _ = QFileDialog.getSaveFileName(self, "保存报告", "PC用电汇总.html", "HTML (*.html)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
            QMessageBox.information(self, "已保存", f"报告已保存到：\n{path}")

    def _save_png(self, html: str) -> bool:
        """弹出保存对话框，把当前报告导出为 PNG 图片。"""
        path, _ = QFileDialog.getSaveFileName(
            self, "保存报告(PNG)", "PC用电汇总.png", "PNG 图片 (*.png)")
        if not path:
            return False
        if not path.lower().endswith(".png"):
            path += ".png"
        ok = self._render_png(html, path)
        if ok:
            QMessageBox.information(self, "已保存", f"PNG 报告已保存到：\n{path}")
        else:
            QMessageBox.warning(self, "导出失败",
                                "PNG 未能生成，请确认路径可写后重试。")
        return ok

    def export_report(self):
        """v18.26 起「导出报告」直接输出 PNG 图片（旧版是 PDF）。"""
        html = self._build_report_html()
        self._save_png(html)

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
    def _sync_settings_to_model(self):
        """把配置类设置同步到功耗模型（电源效率/额定功率/校准），供 load 后统一调用（W5）。"""
        self.model.psu_efficiency = self.psu_eff
        self.model.psu_rating_w = self.psu_rating_w
        self.model.calib_k = self.calib_k
        self.model.calib_idle = self.calib_idle
        self.model.calib_peak = self.calib_peak

    # ---- v18.29+ W3：降级账本（收敛静默异常，给维护者/用户可见信号）----
    def _mark_degraded(self, key, msg):
        """登记一项降级（硬件/落盘/采集失败等）：记日志 + 状态区可见信号。"""
        if key not in self._degraded:
            _log.warning("降级项[%s]: %s", key, msg)
        self._degraded.add(key)
        self._degraded_notes[key] = msg
        self._refresh_degraded_status()

    def _clear_degraded(self, key):
        if key in self._degraded:
            self._degraded.discard(key)
            self._degraded_notes.pop(key, None)
            self._refresh_degraded_status()

    def _degraded_summary(self):
        if not self._degraded:
            return ""
        return "；".join(self._degraded_notes.get(k, k) for k in sorted(self._degraded))

    def _refresh_degraded_status(self):
        """把降级项展示在硬件检测标签上（状态栏可见信号）。标签未建好时仅更新账本。"""
        lbl = getattr(self, "_hw_detect_lbl", None)
        if lbl is None:
            return
        if self._degraded:
            lbl.setText(f"⚠ 部分功能降级：{self._degraded_summary()}")
            lbl.setStyleSheet("font-size:11px;color:#b8860b;")
            lbl.setToolTip("检测到异常但已降级运行；详见日志")
        else:
            lbl.setStyleSheet("font-size:11px;color:#8a94a6;")

    def _save_session(self):
        try:
            data = {
                "energy_wh": self.energy_wh, "peak_wall": self.peak_wall,
                "running_elapsed_ms": self.running_elapsed_ms, "running": self.running,
                "finished": self.finished,                 # v18.29+ W5：配置字段统一经 _SETTINGS_SPEC 写入，新增设置只改 spec 一处
                "sample_ms": self.sample_ms,
                "period_energy_wh": self.period_energy_wh,
                "idle_energy_wh": self.idle_energy_wh,
                "active_energy_wh": self.active_energy_wh,
                "ts": time.time(),
                "hourly": self.hourly,
                "app_cpu_wh": self.app_cpu_wh,
                "mini_visible": self._mini_visible,
                "mini_pos": ([self.mini.x(), self.mini.y()]
                             if getattr(self, "mini", None) is not None else self._mini_pos),
                "mini_on_top": (self.mini.layer_on_top()
                                if getattr(self, "mini", None) is not None
                                else getattr(self, "_mini_on_top", False)),
                "mini_bd": (self.mini.bd_visible()
                            if getattr(self, "mini", None) is not None
                            else getattr(self, "_mini_bd", True)),
                # v18.11 显示器状态与熄屏省电累计
                "disp_manual": self._disp_manual,
                "disp_saved_wh": self._disp_saved_wh,
                "samples": list(self.all_samples)[-2000:],
            }
            # v18.29+ W5：配置字段统一经 _SETTINGS_SPEC 写入，新增设置只改 spec 一处
            for k, _ in _SETTINGS_SPEC:
                v = getattr(self, k)
                if k == "psu_rating_w":
                    v = float(v or 0.0)
                elif k == "tou_valley":
                    v = list(self.tou_valley)
                elif k == "tou_peak":
                    v = [list(x) for x in self.tou_peak]
                data[k] = v
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            # v18.27 不再静默吞异常：会话写盘失败应留痕，便于排查配置丢失
            try:
                print(f"[session] 保存失败: {e!r}")
            except Exception:
                pass
            # v18.29+ W3：落盘类失败必须给用户可见信号，否则配置悄悄丢失
            self._mark_degraded("session_save", "会话保存失败·配置可能未持久化")

    def _load_session_maybe(self):
        if not os.path.exists(SESSION_FILE):
            return
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            # v18.29+ W3：落盘类失败须留痕（此前完全静默，旧会话被悄悄丢弃）
            _log.warning("会话文件解析失败，已忽略旧会话: %s", e)
            self._mark_degraded("session_load", "旧会话读取失败·已重置")
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
        # v18.29+ W5：配置字段统一经 _SETTINGS_SPEC 读取，新增设置只改 spec 一处
        for k, default in _SETTINGS_SPEC:
            v = d.get(k, default)
            if k == "psu_rating_w":
                v = float(v or 0.0)
            elif k == "tou_valley":
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    v = (int(v[0]), int(v[1]))
            elif k == "tou_peak":
                if isinstance(v, list):
                    v = [(int(a), int(b)) for a, b in v]
            setattr(self, k, v)
        self._sync_settings_to_model()   # 把配置同步进功耗模型（效率/校准/额定功率）
        # 运行期累计（非配置），保持显式
        self.period_energy_wh = d.get("period_energy_wh", {"谷": 0.0, "平": 0.0, "峰": 0.0})
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
        self._mini_on_top = bool(d.get("mini_on_top", False))
        self._mini_bd = bool(d.get("mini_bd", True))
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
        # v18.30 修复：QBarSeries 没有 setColor（只有 QBarSet 有），旧写法抛 AttributeError，
        # 窗口化运行时无控制台 → 异常被静默吞掉，表现为「点历史趋势没反应」。
        bar_set.setColor(QColor("#2f6bff"))
        line_series = QLineSeries()
        line_series.setName("电费 ¥")
        line_series.setColor(QColor("#ff8a3d"))
        line_series.setPointsVisible(True)
        for i, r in enumerate(recent):
            bar_set.append(r["kwh"])
            line_series.append(i, r["cost"])

        bars = QBarSeries()
        bars.append(bar_set)
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
    def _billing_settings_dict(self) -> dict:
        """导出计费相关设置，供 PowerEngine.from_settings_dict 构造引擎（单一真相源）。"""
        d = {}
        for k in PC.PowerEngine.BILLING_KEYS:
            v = getattr(self, k, None)
            d[k] = list(v) if isinstance(v, (tuple, list)) else v
        return d

    def _simulate(self, hours_off: float, target_eff: float) -> dict:
        """基于本次监测的平均功耗与当前计费方式，估算两种节能情景的月省电量/电费。

        v18.29+：委托给 power_core.PowerEngine.simulate（消除双实现漂移，见审查报告 §5.2）。
        引擎用本会话的累计电量与当前计费配置构造，结果与原本地计算逐项一致（有 parity 测试守护）。
        """
        eng = PC.PowerEngine.from_settings_dict(self.model, self._billing_settings_dict())
        eng.energy_wh = self.energy_wh
        eng.running_elapsed_ms = self.running_elapsed_ms
        cur = self.cur or {}
        cur_psu_eff = cur.get("psu_eff")
        cur_wall = cur.get("wall") or 0.0
        return eng.simulate(hours_off, target_eff, cur_psu_eff=cur_psu_eff, cur_wall=cur_wall)

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

    def open_chart_detail(self):
        """v18.32：主界面功耗曲线双击进入的大图详情。

        · 完整重画近 60 分钟曲线（数据源 _chart_pts，与主图同一份镜像）；
        · 支持鼠标框选放大（rubber band）、双击复位；
        · 顶部给当前/平均/峰值统计，底部附操作提示。
        """
        d = QDialog(self); d.setWindowTitle("功耗曲线详情（近 60 分钟）"); d.setStyleSheet(CSS)
        d.resize(1020, 600)
        # v18.33 双击进入详情时主界面自动隐藏（详情独占屏幕），
        # 关闭详情后 _show_window() 恢复主界面——避免遮挡，也不存在"关了详情
        # 主界面沉到别的窗口后面找不回"的问题（主窗口是 Tool 型，无任务栏按钮）。
        self.hide()

        def _exec_restore():
            try:
                d.exec()
            finally:
                self._show_window()

        vl = QVBoxLayout(d); vl.setContentsMargins(16, 14, 16, 14); vl.setSpacing(10)

        pts = list(getattr(self, "_chart_pts", None) or [])
        if len(pts) < 2:
            tip = QLabel("暂无曲线数据。开始监测后每 2 秒记录一个采样点，"
                         "稍后再双击曲线即可查看详情。")
            tip.setWordWrap(True); tip.setStyleSheet("font-size:13px;color:#6b7488;")
            vl.addWidget(tip)
            ok = QPushButton("关闭"); ok.clicked.connect(d.accept); vl.addWidget(ok)
            _exec_restore(); return

        # 统计摘要
        walls = [wv for _ts, wv in pts if wv > 0]
        now_w = pts[-1][1]
        avg_w = (sum(walls) / len(walls)) if walls else 0.0
        pk_ts, pk_w = max(pts, key=lambda p: p[1])
        pk_str = QDateTime.fromMSecsSinceEpoch(int(pk_ts)).toString("HH:mm:ss")
        span_min = (pts[-1][0] - pts[0][0]) / 60000.0
        head = QLabel(f"当前 <b>{now_w:.0f} W</b> · 平均 <b>{avg_w:.0f} W</b> · "
                      f"峰值 <b>{pk_w:.0f} W</b>（{pk_str}） · "
                      f"共 {len(pts)} 个采样点，覆盖约 {span_min:.0f} 分钟")
        head.setStyleSheet("font-size:13px;color:#2b3552;")
        vl.addWidget(head)

        series = QLineSeries()
        series.setColor(QColor("#2f6bff"))
        # x 用毫秒时间戳（float）：与主图 _chart_card 相同做法——QDateTimeAxis 接受
        # 毫秒 x 值；QXYSeries.append 不接受 (QDateTime, float) 重载，只能传数值。
        for ts, wv in pts:
            series.append(float(ts), float(wv))
        chart = QChart()
        chart.addSeries(series)
        chart.legend().hide()
        chart.setBackgroundVisible(False)
        ax_x = QDateTimeAxis(); ax_x.setFormat("HH:mm:ss")
        ax_y = QValueAxis(); ax_y.setTitleText("插座功耗 W")
        vals = [wv for _t, wv in pts]
        y_lo = 0.0
        y_hi = max(max(vals) * 1.15, 50.0)
        ax_x.setRange(QDateTime.fromMSecsSinceEpoch(int(pts[0][0])),
                      QDateTime.fromMSecsSinceEpoch(int(pts[-1][0])))
        ax_y.setRange(y_lo, y_hi)
        ax_y.setLabelFormat("%.0f")
        chart.addAxis(ax_x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(ax_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(ax_x); series.attachAxis(ax_y)

        view = ChartView(chart)          # 未给回调 -> 双击复位缩放
        view.setRenderHint(QPainter.RenderHint.Antialiasing)
        view.setRubberBand(QChartView.RubberBand.RectangleRubberBand)
        view.setMinimumHeight(420)
        vl.addWidget(view, 1)

        # 刷新率切换（v18.32）：详情内可改全局采样间隔，经 worker.set_interval
        # 线程安全生效（下一拍应用，不跨线程重启定时器），并随会话落盘。
        rate_row = QHBoxLayout()
        rate_lbl = QLabel("刷新率")
        cb_rate = QComboBox()
        for ms, label in ((1000, "1 秒"), (2000, "2 秒"), (3000, "3 秒"),
                          (5000, "5 秒"), (10000, "10 秒")):
            cb_rate.addItem(label, ms)
        _idx = cb_rate.findData(int(getattr(self, "sample_ms", SAMPLE_MS)))
        cb_rate.setCurrentIndex(max(0, _idx))
        rate_note = QLabel()
        rate_note.setStyleSheet("font-size:12px;color:#8a93a6;")

        def _on_rate(i):
            ms = int(cb_rate.itemData(i))
            self.sample_ms = ms
            wk = getattr(self, "worker", None)
            if wk is not None:
                wk.set_interval(ms)
            rate_note.setText(f"已切换为 {ms/1000:.0f} 秒/拍，主界面与详情同步生效")
            try:
                self._save_session()
            except Exception:
                pass

        cb_rate.currentIndexChanged.connect(_on_rate)
        rate_row.addWidget(rate_lbl)
        rate_row.addWidget(cb_rate)
        rate_row.addWidget(rate_note, 1)
        vl.addLayout(rate_row)

        # 详情曲线实时跟进：每秒把 _chart_pts 中新增的采样点追加到大图上并平移 x 轴，
        # 弹窗不再是打开瞬间的静态快照。timer 挂在 d 上，弹窗销毁即回收。
        sync_state = {"last": pts[-1][0]}
        sync_timer = QTimer(d)
        sync_timer.setInterval(1000)

        def _sync_new_points():
            try:
                added = False
                for ts, wv in (getattr(self, "_chart_pts", None) or []):
                    if ts > sync_state["last"]:
                        series.append(float(ts), float(wv))
                        sync_state["last"] = ts
                        added = True
                if added:
                    latest = (getattr(self, "_chart_pts", None) or [])
                    if latest:
                        ax_x.setMax(QDateTime.fromMSecsSinceEpoch(int(latest[-1][0])))
            except Exception:
                pass      # 已知：弹窗关闭竞态下 self 状态可能变化，静默忽略即可

        sync_timer.timeout.connect(_sync_new_points)
        sync_timer.start()

        hint = QLabel("鼠标左键框选可放大局部 · 双击曲线复位缩放 · 曲线实时跟进新采样点")
        hint.setStyleSheet("font-size:12px;color:#8a93a6;")
        vl.addWidget(hint)
        ok = QPushButton("关闭"); ok.clicked.connect(d.accept); vl.addWidget(ok)
        _exec_restore()

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
        # v18.21：去掉「本轮 」前缀——196px 窗宽内放不下全量文本，金额会被裁；
        # 累计量大时（≥100kWh）降精度，保证长年运行也不超宽
        kwh = self.energy_wh / 1000.0
        if kwh >= 100:
            m.lbl_cost.setText(f"{kwh:.1f} kWh · ¥{cost:.0f}")
        else:
            m.lbl_cost.setText(f"{kwh:.2f} kWh · ¥{cost:,.2f}")
        # v18.24 温度行：CPU/GPU/硬盘（来自采样 sys；缺失时整行收起）
        try:
            sy = getattr(self, "_sys_dyn", None) or {}
            parts = []
            ct = sy.get("cpu_temp")
            if ct:
                parts.append("CPU %d°" % round(ct))
            gt = sy.get("gpu_temp")
            if gt:
                parts.append("GPU %d°" % round(gt))
            dts = sy.get("disk_temps") or {}
            for i, (_k, t) in enumerate(sorted(dts.items())[:2]):
                if t:
                    parts.append(("盘%d %d°" % (i + 1, round(t))) if i else ("盘 %d°" % round(t)))
            m.lbl_temp.setText(" ".join(parts))
        except Exception:
            m.lbl_temp.setText("")
        m._apply_size()
        # v18.17 第四行：功耗构成；v18.36 四列（+使用率、温度/转速）
        try:
            _bd = (self.cur or {}).get("breakdown") or {}
            _util, _temp, _tip = {}, {}, {}
            for _k in _bd:
                _p = self._util_pct(_k)
                _util[_k] = f"{_p:.0f}%" if _p is not None else "—"
                _t, _c = self._temp_text_color(_k)
                _temp[_k] = _t
                _tip[_k] = self._temp_tooltip(_k)
            m.set_breakdown(_bd, _util, _temp, _tip)
        except Exception:
            pass
        # v18.19 右上角配置信息（硬件概要，取自启动时检测的本机配置）
        try:
            if not getattr(self, "_mini_cfg_set", False):
                m.set_config(_cfg_compact(self.hw))
                self._mini_cfg_set = True
        except Exception:
            pass

    def _on_mini_layer(self, on_top: bool):
        """悬浮窗层级被右键切换：记录下来并立即落盘，下次启动保持。"""
        self._mini_on_top = bool(on_top)
        try:
            self._save_session()
        except Exception:
            pass

    def _on_mini_bd(self, on: bool):
        """悬浮窗「功耗构成」开关被右键切换：记录并落盘。"""
        self._mini_bd = bool(on)
        try:
            self._save_session()
        except Exception:
            pass

    def _toggle_mini(self, on: bool):
        """v18.8 开关迷你悬浮窗（托盘菜单/设置面板/会话恢复共用）。"""
        self._mini_visible = bool(on)
        if self.mini is None:
            self.mini = MiniOverlay(on_top=getattr(self, '_mini_on_top', False),
                            show_bd=getattr(self, '_mini_bd', True))
            # 右键切层级 / 右键隐藏 都回落到主窗口，便于同步设置与落盘
            self.mini._layer_cb = self._on_mini_layer
            self.mini._hide_cb = lambda: self._toggle_mini(False)
            self.mini._bd_cb = self._on_mini_bd
        if on:
            if self._mini_pos:
                self.mini.move(int(self._mini_pos[0]), int(self._mini_pos[1]))
            else:
                # v18.19 无历史位置：默认整个落在屏幕右侧 15% 区域
                self.mini.place_right()
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
        a_src = menu.addAction("导出源码（内嵌于 EXE）"); a_src.triggered.connect(self._export_source)
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
        """v18.13 非阻塞通知。

        绝不能退化成模态框：_display_on() 每次采样都会走到这里，
        托盘不可用时弹 QMessageBox 会直接把整个监测冻住
        （离屏回归就是这么挂了 6 分钟）。托盘不可用就只记日志。
        """
        try:
            if self.tray is not None:
                self.tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Warning, 4000)
                return
        except Exception:
            pass
        try:
            print("[notify] %s | %s" % (title, str(body).replace("\n", " ")))
        except Exception:
            pass

    def _on_tray_activate(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            if self.isVisible():
                self.hide()
            else:
                self._show_window()

    def _export_source(self):
        """把内嵌在 exe 里的源码导出到用户选择的目录。
        PyInstaller onefile 运行时源码在 sys._MEIPASS/src；开发 / onedir 下回退到 BASE_DIR 真实源码目录。
        托盘菜单「导出源码（内嵌于 EXE）」入口。"""
        src_root = None
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and os.path.isdir(os.path.join(meipass, "src")):
            src_root = os.path.join(meipass, "src")
        elif os.path.isdir(BASE_DIR):
            src_root = BASE_DIR
        if not src_root:
            QMessageBox.warning(self, "导出失败", "未找到可导出的源码目录。")
            return
        d = QFileDialog.getExistingDirectory(self, "选择源码导出目录", os.path.expanduser("~"))
        if not d:
            return
        try:
            skip_dirs = {"build", "dist", "release", "__pycache__", ".git",
                         ".pytest_cache", ".workbuddy", "venv", "envs"}
            skip_ext = (".exe", ".pyc", ".pyo", ".log", ".lock")
            count = 0
            for root, dirs, files in os.walk(src_root):
                dirs[:] = [x for x in dirs if x not in skip_dirs]
                for fn in files:
                    if fn.lower().endswith(skip_ext):
                        continue
                    if fn.startswith(("_probe_", "_diag_", "_staging", "_MEI")):
                        continue
                    if fn in ("session.json", "history.json", "app.lock"):
                        continue
                    sp = os.path.join(root, fn)
                    rel = os.path.relpath(sp, src_root)
                    dp = os.path.join(d, rel)
                    os.makedirs(os.path.dirname(dp), exist_ok=True)
                    shutil.copy2(sp, dp)
                    count += 1
            QMessageBox.information(self, "导出完成",
                                    f"已将 {count} 个源文件导出到：\n{d}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

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


def _install_excepthook():
    """v18.30：窗口化运行（--windowed）没有控制台，槽函数里的异常会被静默吞掉，
    用户侧表现为「点了按钮没反应」（本版历史趋势即因此失效未被发现）。
    这里把未捕获异常同时写日志 + 弹一次错误框，让故障立即可见（同 W3 目标）。"""
    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            _log.error("未捕获异常：\n%s", text)
        except Exception:
            pass
        try:
            print("[uncaught]", text, file=sys.stderr)
        except Exception:
            pass
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                box = QMessageBox(
                    QMessageBox.Icon.Critical, "程序异常",
                    f"发生未捕获异常，本次操作可能未生效：\n\n"
                    f"{exc_type.__name__}: {exc}\n\n（详情已写入日志，15 秒后自动关闭）",
                    QMessageBox.StandardButton.Ok)
                box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
                QTimer.singleShot(15000, box.close)
                box.exec()
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _hook


def main():
    _install_excepthook()
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
    # v18.28 真实显示器电源事件：窗口原生句柄(winId)就绪后注册 GUID_MONITOR_POWER_ON
    # 通知。tray 模式 w.hide() 后仍可创建隐藏原生窗口句柄，钩子照常生效。
    w._install_monitor_power_hook()
    # 首次刷新明细表
    w._refresh_breakdown()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
