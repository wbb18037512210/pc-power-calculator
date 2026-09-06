# -*- coding: utf-8 -*-
"""v18.38 补丁：
A) GPU 使用率改真实占用（nvidia-smi utilization.gpu / LHM GPU Core Load），
   不再用「功耗÷TDP」估算（用户反馈与任务管理器差距大）。
B) 内存温度：LHM 的 DIMM 硬件是 /memory/dimm/*，此前只映射 /ram，
   有 SPD 探头的内存会被漏掉；无探头时 UI 明确说明原因。
"""
import io, os

ROOT = os.path.dirname(os.path.abspath(__file__))


def rw(name):
    p = os.path.join(ROOT, name)
    return p, io.open(p, "r", encoding="utf-8").read()


def save(p, s):
    io.open(p, "w", encoding="utf-8").write(s)


# ============================== hardware.py ==============================
p, s = rw("hardware.py")

# --- A1) 常驻 nvidia-smi 流增加 utilization.gpu --------------------------
old = '''                    ["nvidia-smi", "--query-gpu=power.draw,temperature.gpu",'''
new = '''                    # v18.38 增加 utilization.gpu：GPU 使用率取真实 SM 占用，
                    # 不再用「功耗 ÷ TDP」估算（后者空载也有 8~10%，与任务管理器差很远）
                    ["nvidia-smi",
                     "--query-gpu=power.draw,temperature.gpu,utilization.gpu",'''
if new not in s:
    assert old in s, "hardware: nvidia-smi 命令未找到"
    s = s.replace(old, new, 1)

# --- A2) _gpu_val 元组扩一位（功耗, 温度, 占用率, valid, ts） -------------
old = '''        self._gpu_val = (None, None, False, 0.0)  # (功耗W, GPU温度°C, valid, ts)'''
new = '''        # (功耗W, GPU温度°C, GPU占用率%, valid, ts)
        self._gpu_val = (None, None, None, False, 0.0)'''
if new not in s:
    assert old in s, "hardware: _gpu_val 初始化未找到"
    s = s.replace(old, new, 1)

old = '''                try:
                    parts = s.split(",")
                    v = float(parts[0])
                    temp = float(parts[1]) if len(parts) > 1 else None
                    with self._lock:
                        self._gpu_val = (v, temp, True, time.time())
                except Exception:
                    continue'''
new = '''                try:
                    parts = s.split(",")
                    v = float(parts[0])
                    temp = float(parts[1]) if len(parts) > 1 else None
                    util = None
                    if len(parts) > 2:
                        try:
                            util = max(0.0, min(100.0, float(parts[2])))
                        except ValueError:
                            util = None
                    with self._lock:
                        self._gpu_val = (v, temp, util, True, time.time())
                except Exception:
                    continue'''
if new not in s:
    assert old in s, "hardware: _gpu_reader 未找到"
    s = s.replace(old, new, 1)

old = '''        with self._lock:
            gpu_p, gpu_t, _gpu_ok, gpu_ts = self._gpu_val'''
new = '''        with self._lock:
            gpu_p, gpu_t, gpu_u, _gpu_ok, gpu_ts = self._gpu_val'''
if new not in s:
    assert old in s, "hardware: sample() 解包未找到"
    s = s.replace(old, new, 1)

old = '''            "gpu_power": gpu_p if (self.gpu_nvidia and gpu_fresh) else None,
            "gpu_valid": bool(self.gpu_nvidia and gpu_fresh),'''
new = '''            "gpu_power": gpu_p if (self.gpu_nvidia and gpu_fresh) else None,
            "gpu_util": gpu_u if (self.gpu_nvidia and gpu_fresh) else None,
            "gpu_valid": bool(self.gpu_nvidia and gpu_fresh),'''
if new not in s:
    assert old in s, "hardware: sample() 返回未找到"
    s = s.replace(old, new, 1)

# --- A3) LHM 的 GPU Core 占用（非 N 卡 / nvidia-smi 不可用时的真实来源） ---
old = '''def lhm_probe(kind: str):'''
new = '''def lhm_gpu_load():
    """LHM 的 GPU Core 占用率（%）：非 N 卡或 nvidia-smi 不可用时的真实占用来源。

    与任务管理器「GPU 利用率」同一口径（LHM 对 N 卡读 NVML、A 卡读 ADL/GPU 节点）。
    """
    for g in _LHM_SENS.get("groups") or []:
        if not (g.get("hwid") or "").startswith("/gpu"):
            continue
        for sn in g.get("sensors") or []:
            if sn.get("type") != "Load":
                continue
            low = (sn.get("name") or "").lower()
            if low == "gpu core" or (low.startswith("gpu core") and "memory" not in low):
                try:
                    v = float((sn.get("value") or "").split()[0])
                except (ValueError, IndexError):
                    return None
                return max(0.0, min(100.0, v))
    return None


def lhm_probe(kind: str):'''
if new not in s:
    assert old in s, "hardware: lhm_probe 定义未找到"
    s = s.replace(old, new, 1)

# --- B) 内存温度：DIMM 硬件前缀 /memory/dimm/* 也要归入 memory -----------
old = '''    ("/ram", "memory"),'''
new = '''    ("/ram", "memory"), ("/memory", "memory"),   # DIMM 是 /memory/dimm/N'''
if new not in s:
    assert old in s, "hardware: _LHM_KIND_PREFIX 未找到"
    s = s.replace(old, new, 1)

save(p, s)
h = io.open(p, "r", encoding="utf-8").read()
assert "utilization.gpu" in h and "def lhm_gpu_load" in h and '"/memory", "memory"' in h
print("hardware.py OK")

# ================================ main.py ================================
p, s = rw("main.py")

# --- A4) _util_pct：真实占用优先，功耗/TDP 仅兜底 ------------------------
old = '''        if "GPU" in n:
            gp = self.cur.get("gpu_power") if isinstance(self.cur, dict) else None
            tdp = float(getattr(self.model, "gpu_tdp", 0) or 0)
            if gp is not None and tdp > 0:
                return min(100.0, float(gp) / tdp * 100)
            return None'''
new = '''        if "GPU" in n:
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
            return None'''
if new not in s:
    assert old in s, "main: _util_pct GPU 分支未找到"
    s = s.replace(old, new, 1)

# --- A5) 进度条 tooltip 标注来源 -----------------------------------------
old = '''            pb.setToolTip(tip)'''
new = '''            pb.setToolTip(tip)'''
if new not in s:
    pass

# 在 _update_util_bar 末尾给 GPU 行加来源说明
old = '''    def _refresh_breakdown(self):'''
new = '''    def _util_tip(self, name):
        """v18.38 使用率列的来源说明：GPU 行标注占用率是实测还是功耗估算。"""
        n = str(name).upper()
        if "GPU" in n:
            src = getattr(self, "_gpu_util_src", None)
            if src == "nvidia-smi":
                return "GPU 使用率：nvidia-smi 实测 SM 占用（与任务管理器同口径）"
            if src == "LHM":
                return "GPU 使用率：LibreHardwareMonitor 的 GPU Core 占用"
            if src == "估算":
                return ("GPU 使用率：由「功耗 ÷ TDP」估算\\n"
                        "未取到实测占用（nvidia-smi 不可用且非 LHM 可识别显卡）")
        return ""

    def _refresh_breakdown(self):'''
if new not in s:
    assert old in s, "main: _refresh_breakdown 未找到"
    s = s.replace(old, new, 1)

# 重建/差异更新时给使用率单元格设 tooltip
old = '''                item.setText(f"{bd[name]:.1f}")
                self._update_util_bar(pb, name)'''
new = '''                item.setText(f"{bd[name]:.1f}")
                self._update_util_bar(pb, name)
                pb.setToolTip(self._util_tip(name))'''
if new not in s:
    assert old in s, "main: 差异更新分支未找到"
    s = s.replace(old, new, 1)

old = '''            cell = self._make_util_cell()
            self._update_util_bar(cell, name)
            self.table.setCellWidget(r, 1, cell)'''
new = '''            cell = self._make_util_cell()
            self._update_util_bar(cell, name)
            cell.setToolTip(self._util_tip(name))
            self.table.setCellWidget(r, 1, cell)'''
if new not in s:
    assert old in s, "main: 重建分支未找到"
    s = s.replace(old, new, 1)

# --- B) 内存无温度探头时给出明确说明（而不是孤零零一个 —） ---------------
old = '''            return ("LibreHardwareMonitor 未报告「%s」的温度/转速\\n"
                    "该部件本身通常不带温度探头（属正常现象）" % s)'''
new = '''            if "内存" in s:
                return ("内存温度读不到：本机的内存条没有 SPD 温度探头\\n"
                        "（LHM 只能读到容量与 SPD 时序；DDR5 / 部分高端 DDR4 才有温度探头）\\n"
                        "这是硬件限制，HWiNFO、AIDA64 等同样读不到")
            return ("LibreHardwareMonitor 未报告「%s」的温度/转速\\n"
                    "该部件本身通常不带温度探头（属正常现象）" % s)'''
if new not in s:
    assert old in s, "main: tooltip 无读数分支未找到"
    s = s.replace(old, new, 1)

# --- B2) 传感器详情窗口：可显示全部类型（便于查看内存 SPD 时序） ---------
old = '''        TYPES = ("Temperature", "Fan", "Control", "Power", "Clock", "Load")
        d = QDialog(self)'''
new = '''        TYPES = ("Temperature", "Fan", "Control", "Power", "Clock", "Load")
        TYPES_ALL = TYPES + ("Data", "Timing", "Voltage", "Current",
                             "Level", "Throughput", "Factor", "SmallData")
        d = QDialog(self)'''
if new not in s:
    assert old in s, "main: TYPES 定义未找到"
    s = s.replace(old, new, 1)

old = '''        def _fill():
            try:
                snap = H.lhm_sensors(force=True)
            except Exception:
                snap = {"ok": False, "groups": []}
            tree.setRowCount(0)
            n = 0
            for g in snap.get("groups") or []:
                ss = [x for x in (g.get("sensors") or []) if x.get("type") in TYPES]'''
new = '''        _show_all = {"v": False}

        def _fill():
            try:
                snap = H.lhm_sensors(force=True)
            except Exception:
                snap = {"ok": False, "groups": []}
            tree.setRowCount(0)
            n = 0
            _ty = TYPES_ALL if _show_all["v"] else TYPES
            for g in snap.get("groups") or []:
                ss = [x for x in (g.get("sensors") or []) if x.get("type") in _ty]'''
if new not in s:
    assert old in s, "main: _fill 未找到"
    s = s.replace(old, new, 1)

old = '''        bar = QHBoxLayout()
        btn_ref = QPushButton("刷新")
        btn_ref.setObjectName("ghost")'''
new = '''        bar = QHBoxLayout()
        ck_all = QCheckBox("显示全部传感器（含内存 SPD 时序/容量）")
        ck_all.toggled.connect(lambda v: (_show_all.__setitem__("v", v), _fill()))
        bar.addWidget(ck_all)
        bar.addStretch(1)
        btn_ref = QPushButton("刷新")
        btn_ref.setObjectName("ghost")'''
if new not in s:
    assert old in s, "main: 按钮栏未找到"
    s = s.replace(old, new, 1)

# 版本号
old = 'APP_VERSION = "v18.37"'
new = 'APP_VERSION = "v18.38"'
if new not in s:
    assert old in s, "main: 版本号未找到"
    s = s.replace(old, new, 1)

save(p, s)
m = io.open(p, "r", encoding="utf-8").read()
assert 'APP_VERSION = "v18.38"' in m
assert "def lhm_gpu_load" in m or "lhm_gpu_load()" in m
assert "没有 SPD 温度探头" in m
assert "显示全部传感器" in m
print("main.py OK")
