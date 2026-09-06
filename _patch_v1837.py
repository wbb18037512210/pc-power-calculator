# -*- coding: utf-8 -*-
"""v18.37 补丁：以 LibreHardwareMonitor 为参考显示温度/转速。

幂等：已打过的片段会跳过（用 `new in s` 判断），末尾做 assert 校验。
Edit 工具在沙箱里曾出现「返回成功但未落盘」，故改用脚本改盘 + 复核。
"""
import io, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def rw(name):
    p = os.path.join(ROOT, name)
    with io.open(p, "r", encoding="utf-8") as f:
        return p, f.read()


def save(p, s):
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(s)


# ============================ hardware.py ============================
p, s = rw("hardware.py")

# 1) 语义温度候选增加 GPU（与 LHM 面板 GPU Core 对齐）
old = '    cands = {"cpu": [], "memory": [], "motherboard": []}'
new = '    cands = {"cpu": [], "memory": [], "motherboard": [], "gpu": []}'
if new not in s:
    assert old in s, "hardware: cands 未找到"
    s = s.replace(old, new, 1)

# 2) walk 里识别 /gpu-* 硬件
old = '''        elif hwid.startswith("/ram"):
            kind = "ram"'''
new = '''        elif hwid.startswith("/ram"):
            kind = "ram"
        elif hwid.startswith("/gpu"):
            kind = "gpu"'''
if new not in s:
    assert old in s, "hardware: ram 判定未找到"
    s = s.replace(old, new, 1)

# 3) 温度分类：增加 GPU 通道（GPU Core > Hot Spot > 其他）
old = '''                    elif kind == "board":
                        cands["motherboard"].append((0, v))'''
new = '''                    elif kind == "gpu":
                        # 与 LHM 面板一致：主温度取 GPU Core，其次 Hot Spot
                        gl = name.lower()
                        prio = 0 if "core" in gl else (1 if "hot" in gl else 2)
                        cands["gpu"].append((prio, v))
                    elif kind == "board":
                        cands["motherboard"].append((0, v))'''
if new not in s:
    assert old in s, "hardware: board 分支未找到"
    s = s.replace(old, new, 1)

# 4) 解析完顺便缓存「与 LHM 界面同构」的完整传感器树
old = '''    walk(data, "")
    temps = {}
    for key, lst in cands.items():
        if lst:
            lst.sort(key=lambda x: x[0])
            temps[key] = round(lst[0][1], 1)
    return fans, temps, True'''
new = '''    walk(data, "")
    temps = {}
    for key, lst in cands.items():
        if lst:
            lst.sort(key=lambda x: x[0])
            temps[key] = round(lst[0][1], 1)
    # v18.37 同时缓存完整传感器树（供 UI 按 LHM 的分组/命名展示原始读数）
    try:
        _LHM_SENS.update(groups=_build_lhm_tree(data), ts=time.time(), ok=True)
    except Exception:
        pass
    return fans, temps, True


# ---------------------------------------------------------------------------
# v18.37 LHM 完整传感器快照 —— 让 UI 能「以 LibreHardwareMonitor 为参考」显示
# 温度/转速：分组、命名、Min/Value/Max 全部照搬 LHM 的 Web Server 输出。
# ---------------------------------------------------------------------------
_LHM_SENS = {"ts": 0.0, "ok": False, "groups": []}


def _build_lhm_tree(root) -> list:
    """把 LHM /data.json 还原成与它界面一致的「硬件 → 传感器」两级结构。

    硬件节点  = 带 HardwareId（如 /amdcpu/0、/lpc/nct6793d/0、/gpu-nvidia/0）
    传感器节点 = 带 SensorId（Text/Min/Value/Max 均带单位，原样保留）
    """
    groups = []

    def visit(node, cur):
        hwid = node.get("HardwareId")
        sid = node.get("SensorId")
        if hwid:
            cur = {"name": str(node.get("Text") or ""), "hwid": str(hwid),
                   "sensors": []}
            groups.append(cur)
        elif sid and cur is not None:
            cur["sensors"].append({
                "name": str(node.get("Text") or ""),
                "type": str(node.get("Type") or ""),
                "min": str(node.get("Min") or ""),
                "value": str(node.get("Value") or ""),
                "max": str(node.get("Max") or ""),
                "sid": str(sid),
            })
        for c in node.get("Children") or []:
            visit(c, cur)

    visit(root, None)
    return groups


def lhm_sensors(force: bool = False) -> dict:
    """返回 {"ok", "ts", "groups"}：与 LHM 面板同构的全部传感器（含 Min/Max）。

    force=True 会立即重新拉一次 /data.json（普通采样有 10s 缓存）。
    """
    if force or not _LHM_SENS.get("groups"):
        sensor_snapshot_cached(force=True)
    return dict(_LHM_SENS)


# 硬件 HardwareId 前缀 → 部件类别
_LHM_KIND_PREFIX = (
    ("/amdcpu", "cpu"), ("/intelcpu", "cpu"),
    ("/lpc", "motherboard"), ("/motherboard", "motherboard"),
    ("/ram", "memory"),
    ("/gpu", "gpu"),
    ("/nvme", "disk"), ("/hdd", "disk"),
)


def lhm_probe(kind: str):
    """取 LHM 中某类部件的全部温度/转速读数（只读缓存，不触发网络）。

    kind: cpu | motherboard | memory | gpu | disk | fan
      fan → 所有当前转速 > 0 的风扇（含显卡风扇）
      其余 → 对应硬件分组下的 Temperature（5–100°C 过滤 SuperIO 坏通道）
    返回 [("硬件名 · 传感器名", "原始值文本"), ...]，顺序与 LHM 面板一致。
    """
    out = []
    for g in _LHM_SENS.get("groups") or []:
        hwid = g.get("hwid") or ""
        kind_of = ""
        for pfx, k in _LHM_KIND_PREFIX:
            if hwid.startswith(pfx):
                kind_of = k
                break
        for sn in g.get("sensors") or []:
            tp = sn.get("type")
            try:
                v = float((sn.get("value") or "").split()[0])
            except (ValueError, IndexError):
                continue
            if kind == "fan":
                if tp != "Fan" or v <= 0:
                    continue
            else:
                if tp != "Temperature" or kind_of != kind:
                    continue
                if not (5.0 <= v <= 100.0):
                    continue
            out.append((f"{g.get('name')} · {sn.get('name')}",
                        sn.get("value") or ""))
    return out'''
if new not in s:
    assert old in s, "hardware: _parse_lhm_json 结尾未找到"
    s = s.replace(old, new, 1)

save(p, s)
assert "_LHM_SENS" in s and "def lhm_probe" in s and '"/gpu", "gpu"' in s
print("hardware.py OK")

# ============================== main.py ==============================
p, s = rw("main.py")

# 1) 版本号
old = 'APP_VERSION = "v18.36"'
new = 'APP_VERSION = "v18.37"'
if new not in s:
    assert old in s, "main: 版本号未找到"
    s = s.replace(old, new, 1)

# 2) GPU 温度回退 LHM
old = '''        elif "GPU" in n:
            t = dyn.get("gpu_temp")
            warn, hot = self._TEMP_LIMITS["gpu"]'''
new = '''        elif "GPU" in n:
            t = dyn.get("gpu_temp")
            if t is None:
                t = st.get("gpu")          # v18.37 回退 LHM（GPU Core）
            warn, hot = self._TEMP_LIMITS["gpu"]'''
if new not in s:
    assert old in s, "main: GPU 温度分支未找到"
    s = s.replace(old, new, 1)

# 3) 新增 _temp_tooltip（紧跟 _temp_text_color 之后）
old = '''        col = QColor("#1a1a1a") if t < warn else (
            QColor("#d99a17") if t < hot else QColor("#d8492f"))
        return f"{t:.0f}°", col
'''
new = '''        col = QColor("#1a1a1a") if t < warn else (
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
            return ("LibreHardwareMonitor 无该项读数\\n"
                    "（Windows 免驱读不到 SuperIO/EC，需运行 %s）" % H._LHM_EXE)
        return ("LibreHardwareMonitor 参考读数\\n"
                + "\\n".join("• %s：%s" % (a, b) for a, b in rows[:14]))

    def open_sensors(self):
        """v18.37 传感器详情：照 LHM 的分组方式列出温度/风扇/控制等原始读数。"""
        TYPES = ("Temperature", "Fan", "Control", "Power", "Clock", "Load")
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

        def _fill():
            try:
                snap = H.lhm_sensors(force=True)
            except Exception:
                snap = {"ok": False, "groups": []}
            tree.setRowCount(0)
            n = 0
            for g in snap.get("groups") or []:
                ss = [x for x in (g.get("sensors") or []) if x.get("type") in TYPES]
                if not ss:
                    continue
                r = tree.rowCount()
                tree.insertRow(r)
                h = QTableWidgetItem(str(g.get("name") or ""))
                f = h.font()
                f.setBold(True)
                h.setFont(f)
                h.setBackground(QBrush(QColor("#eef1f7")))
                tree.setItem(r, 0, h)
                tree.setSpan(r, 0, 1, 4)
                for x in ss:
                    r = tree.rowCount()
                    tree.insertRow(r)
                    tree.setItem(r, 0, QTableWidgetItem("    " + str(x.get("name") or "")))
                    tree.setItem(r, 1, QTableWidgetItem(str(x.get("min") or "")))
                    _v = QTableWidgetItem(str(x.get("value") or ""))
                    _vf = _v.font()
                    _vf.setBold(True)
                    _v.setFont(_vf)
                    tree.setItem(r, 2, _v)
                    tree.setItem(r, 3, QTableWidgetItem(str(x.get("max") or "")))
                    n += 1
            if n:
                tip.setText("数据源：LibreHardwareMonitor Web Server（127.0.0.1:8085）。"
                            "分组、命名与「最小 / 当前 / 最大」三列与其界面一致。"
                            "灰色分组行 = 该硬件（LHM 中的节点名）。")
            else:
                tip.setText("未读取到 LibreHardwareMonitor 数据。请确认 "
                            "D:\\\\tools\\\\LibreHardwareMonitor\\\\LibreHardwareMonitor.exe 已运行，"
                            "并已开启 Web Server（选项 → Web Server → 端口 8085、Run）。")

        _fill()
        bar = QHBoxLayout()
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
'''
if new not in s:
    assert old in s, "main: _temp_text_color 结尾未找到"
    s = s.replace(old, new, 1)

# 4) 构成表：双击温度列
old = '''        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay.addWidget(self.table, 1)'''
new = '''        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # v18.37 双击「温度/转速」列 → 打开 LibreHardwareMonitor 传感器详情
        self.table.cellDoubleClicked.connect(self._on_table_dbl)
        lay.addWidget(self.table, 1)'''
if new not in s:
    assert old in s, "main: 构成表构造未找到"
    s = s.replace(old, new, 1)

# 5) 温度列 tooltip（重建分支 + 差异更新分支）
old = '''                txt, col = self._temp_text_color(name)
                ti.setText(txt)
                ti.setForeground(col if col is not None else self._bd_temp_gray)
            return'''
new = '''                txt, col = self._temp_text_color(name)
                ti.setText(txt)
                ti.setForeground(col if col is not None else self._bd_temp_gray)
                ti.setToolTip(self._temp_tooltip(name))
            return'''
if new not in s:
    assert old in s, "main: 差异更新分支未找到"
    s = s.replace(old, new, 1)

old = '''            txt, col = self._temp_text_color(name)
            ti = QTableWidgetItem(txt)
            ti.setForeground(col if col is not None else self._bd_temp_gray)
            self.table.setItem(r, 3, ti)'''
new = '''            txt, col = self._temp_text_color(name)
            ti = QTableWidgetItem(txt)
            ti.setForeground(col if col is not None else self._bd_temp_gray)
            ti.setToolTip(self._temp_tooltip(name))
            self.table.setItem(r, 3, ti)'''
if new not in s:
    assert old in s, "main: 重建分支未找到"
    s = s.replace(old, new, 1)

# 6) 控制条加「传感器」按钮（9 → 10 个，仍一行平铺）
old = '''        self.btn_apps = QPushButton("软件耗电"); self.btn_apps.setObjectName("ghost")
        self.btn_apps.clicked.connect(self.open_apps)'''
new = '''        self.btn_apps = QPushButton("软件耗电"); self.btn_apps.setObjectName("ghost")
        self.btn_apps.clicked.connect(self.open_apps)
        # v18.37 传感器详情（以 LibreHardwareMonitor 为参考的温度/转速对照）
        self.btn_sensors = QPushButton("传感器"); self.btn_sensors.setObjectName("ghost")
        self.btn_sensors.clicked.connect(self.open_sensors)'''
if new not in s:
    assert old in s, "main: 软件耗电按钮未找到"
    s = s.replace(old, new, 1)

old = '''        _btns = (self.btn_reset, self.btn_export, self.btn_csv, self.btn_compare,
                 self.btn_history, self.btn_sim, self.btn_hourly, self.btn_apps,
                 self.btn_settings)'''
new = '''        _btns = (self.btn_reset, self.btn_export, self.btn_csv, self.btn_compare,
                 self.btn_history, self.btn_sim, self.btn_hourly, self.btn_apps,
                 self.btn_sensors, self.btn_settings)'''
if new not in s:
    assert old in s, "main: _btns 未找到"
    s = s.replace(old, new, 1)

# 7) 悬浮窗：温度列加 tooltip + 列宽放宽防 "1942 RPM" 截断
old = '''    def _bd_row(self, name: str, watts: float, total: bool = False,
                util_text: str = None, temp_text: str = None):'''
new = '''    def _bd_row(self, name: str, watts: float, total: bool = False,
                util_text: str = None, temp_text: str = None,
                temp_tip: str = None):'''
if new not in s:
    assert old in s, "main: _bd_row 签名未找到"
    s = s.replace(old, new, 1)

old = '''        lay.addWidget(_lbl(temp_text if temp_text else "", "#9fb4d8", 40))
        return row'''
new = '''        _tl = _lbl(temp_text if temp_text else "", "#9fb4d8", 48)
        if temp_tip:
            _tl.setToolTip(temp_tip)
        lay.addWidget(_tl)
        return row'''
if new not in s:
    assert old in s, "main: 悬浮窗温度列未找到"
    s = s.replace(old, new, 1)

old = '''    def set_breakdown(self, bd: dict, util: dict = None, temps: dict = None):
        """v18.22 票据式功耗结构，v18.36 扩展四列（+使用率、温度/转速）。
        util/temps：{部件名: 显示文本}；缺省该列留空。"""
        util = util or {}
        temps = temps or {}'''
new = '''    def set_breakdown(self, bd: dict, util: dict = None, temps: dict = None,
                      temps_tip: dict = None):
        """v18.22 票据式功耗结构，v18.36 扩展四列（+使用率、温度/转速）。
        util/temps：{部件名: 显示文本}；temps_tip：温度列的 LHM 参考读数提示。"""
        util = util or {}
        temps = temps or {}
        temps_tip = temps_tip or {}'''
if new not in s:
    assert old in s, "main: set_breakdown 签名未找到"
    s = s.replace(old, new, 1)

old = '''                _row = self._bd_row(k, v, util_text=util.get(k),
                                    temp_text=temps.get(k))'''
new = '''                _row = self._bd_row(k, v, util_text=util.get(k),
                                    temp_text=temps.get(k),
                                    temp_tip=temps_tip.get(k))'''
if new not in s:
    assert old in s, "main: set_breakdown 行构造未找到"
    s = s.replace(old, new, 1)

# 8) 悬浮窗调用处：传入 LHM 参考读数
old = '''            _bd = (self.cur or {}).get("breakdown") or {}
            _util, _temp = {}, {}
            for _k in _bd:
                _p = self._util_pct(_k)
                _util[_k] = f"{_p:.0f}%" if _p is not None else "—"
                _t, _c = self._temp_text_color(_k)
                _temp[_k] = _t
            m.set_breakdown(_bd, _util, _temp)'''
new = '''            _bd = (self.cur or {}).get("breakdown") or {}
            _util, _temp, _tip = {}, {}, {}
            for _k in _bd:
                _p = self._util_pct(_k)
                _util[_k] = f"{_p:.0f}%" if _p is not None else "—"
                _t, _c = self._temp_text_color(_k)
                _temp[_k] = _t
                _tip[_k] = self._temp_tooltip(_k)
            m.set_breakdown(_bd, _util, _temp, _tip)'''
if new not in s:
    assert old in s, "main: 悬浮窗构成调用未找到"
    s = s.replace(old, new, 1)

save(p, s)
assert 'APP_VERSION = "v18.37"' in s
assert "def open_sensors" in s and "def _temp_tooltip" in s
assert "self.btn_sensors" in s and "lhm_probe" in s
print("main.py OK")
