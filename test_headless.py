# -*- coding: utf-8 -*-
"""无界面冒烟测试：offscreen 平台下真实构建窗口并跑通采样/累加/报告链路。"""
import os, sys, time, tempfile, shutil
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main
# 测试隔离（重要！）：把全部落盘路径重定向到临时目录。
# 旧版本在这里直接删 session.json / history.json —— 一旦后台正跑着真实实例，
# 跑一次冒烟测试就会把用户累计的电量、小时桶历史全部抹掉（已踩过一次）。
_TMPDIR = tempfile.mkdtemp(prefix="pwrmon_test_")
main.BASE_DIR = _TMPDIR
main.SESSION_FILE = os.path.join(_TMPDIR, "session.json")
main.HISTORY_FILE = os.path.join(_TMPDIR, "history.json")
main.LOCK_FILE = os.path.join(_TMPDIR, "app.lock")
import atexit as _atexit
_atexit.register(lambda: shutil.rmtree(_TMPDIR, ignore_errors=True))
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)
w = main.MainWindow()
w.show()

# 模拟运行：开启累计，手动喂几帧采样
w.running = True
w.last_ts = time.time()
for i in range(6):
    w.on_sample({"cpu_load": 40 + i * 5, "gpu_power": 100 + i * 8, "gpu_valid": True})
    time.sleep(0.06)

print("energy_wh =", round(w.energy_wh, 6), "(应 > 0)")
print("cur wall  =", w.cur["wall"], "W")
print("cur sys   =", w.cur["sys"], "W")
print("breakdown rows =", w.table.rowCount())
html = w._build_report_html()
print("report html len =", len(html), "| contains kWh:", "kWh" in html, "| contains 电费:", "电费" in html)
print("chart points =", w.series.count())
w._save_session()
print("session saved =", os.path.exists(main.SESSION_FILE))

# 校准分支验证：两点线性映射（待机/满载实测）
w.model.calib_idle = 80.0
w.model.calib_peak = 400.0
est_idle = main.PM.estimate(w.model, 0, None, False)   # 空载
est_peak = main.PM.estimate(w.model, 100, 180, True)   # 满载
# v18.11 口径：显示器走市电直供，不参与电源效率折算
_mon = float(w.model.components.get("显示器", 0.0) or 0.0)
_eff = w.model.psu_efficiency
# 语义：calib_idle/calib_peak 为「主机」实测插墙功耗，显示器另计
print("calib idle wall =", est_idle["wall_watts"], f"(应≈80/eff+{_mon})")
print("calib peak wall =", est_peak["wall_watts"], f"(应≈400/eff+{_mon})")
assert abs(est_idle["wall_watts"] - (80.0 / _eff + _mon)) < 1.0
assert abs(est_peak["wall_watts"] - (400.0 / _eff + _mon)) < 1.0

# v18.11 显示器开关：关屏后插墙功耗应正好下降一个显示器的量
est_on = main.PM.estimate(w.model, 50, 47.0, True, True)
est_off = main.PM.estimate(w.model, 50, 47.0, True, False)
print("display on wall =", est_on["wall_watts"], "| off =", est_off["wall_watts"],
      "| monitor =", _mon)
assert abs((est_on["wall_watts"] - est_off["wall_watts"]) - _mon) < 0.2
assert est_off["breakdown"]["显示器"] == 0.0
assert hasattr(main.H, "display_auto_off"), "hardware 需提供 display_auto_off"
assert isinstance(main.H.user_idle_sec(), float), "user_idle_sec 应返回 float"
# 手动「关」需在长时间空闲下才成立（有操作会自动恢复），故先固定空闲时长
_orig_idle0 = main.H.user_idle_sec
try:
    main.H.user_idle_sec = lambda: 600.0
    w._disp_manual = False
    assert w._display_on() is False, "手动覆盖为关且空闲久时应返回 False"
    w._disp_manual = True
    assert w._display_on() is True, "手动覆盖为开时应返回 True"
    w._disp_manual = None
    assert isinstance(w._display_on(), bool), "自动模式应返回 bool"
finally:
    main.H.user_idle_sec = _orig_idle0
print("display state toggle OK")

# v18.11 手动标记熄屏后的自动恢复：有键鼠操作应恢复按开屏计，
# 避免用户回来后忘记切回造成长期低估
_orig_idle = main.H.user_idle_sec
try:
    main.H.user_idle_sec = lambda: 5.0        # 刚操作过
    w._disp_manual = False
    assert w._display_on() is True, "空闲短（有操作）时应自动恢复按开屏计"
    main.H.user_idle_sec = lambda: 600.0      # 长时间空闲
    assert w._display_on() is False, "长时间空闲且手动标记关闭时应按熄屏计"
    w._disp_manual = True
    assert w._display_on() is True, "手动标记开启时不受空闲时长影响"
    w._disp_manual = None
finally:
    main.H.user_idle_sec = _orig_idle
print("display auto-wake OK")

# v18.11 设置面板显示器状态下拉：三态选择 -> 保存生效
w.open_settings()
_dcb = w._sw["disp"]
assert _dcb.count() == 3, "显示器状态应有 3 个选项"
_dcb.setCurrentIndex(2)          # 记为关闭
w._save_settings_panel()
assert w._disp_manual is False, "保存后应写入手动关闭"
_dcb.setCurrentIndex(1)          # 记为开启
w._save_settings_panel()
assert w._disp_manual is True, "保存后应写入手动开启"
_dcb.setCurrentIndex(0)          # 自动检测
w._save_settings_panel()
assert w._disp_manual is None, "保存后应回到自动检测"
print("settings display combo OK")

# v18.11 心跳接管：僵尸进程持有互斥体但写不了心跳，
# 心跳过期后新实例必须能接管（否则每次启动都被误判"已在运行"而阻塞）
main._beat()
assert main._last_beat() > 0, "心跳写入后应可读到非零时间戳"
assert (main.time.time() - main._last_beat()) < 5, "刚写入的心跳应判定为新鲜"
_fresh0 = main.LOCK_FRESH_SEC
try:
    main.LOCK_FRESH_SEC = -1.0        # 强制判定心跳过期
    assert main._acquire_single_instance("__hb_probe_mutex__") is True, \
        "心跳过期时应允许新实例接管"
finally:
    main.LOCK_FRESH_SEC = _fresh0
print("heartbeat takeover OK")

# 单系数倍率验证
w.model.calib_idle = 0.0; w.model.calib_peak = 0.0; w.model.calib_k = 1.20
est_k = main.PM.estimate(w.model, 50, 120, True)
print("calib k=1.2 wall =", est_k["wall_watts"])

# 峰谷分时计费验证
w.price_mode = "峰谷"
w.rate_valley, w.rate_flat, w.rate_peak = 0.30, 0.56, 0.85
w.period_energy_wh = {"谷": 1000.0, "平": 2000.0, "峰": 500.0}  # Wh
cost_tou = w._current_cost()
expected = 1.0 * 0.30 + 2.0 * 0.56 + 0.5 * 0.85
print("TOU cost =", round(cost_tou, 3), "expected =", round(expected, 3))
assert abs(cost_tou - expected) < 0.01
from datetime import datetime as _dt
for hh, exp in [(2, "谷"), (9, "峰"), (14, "平"), (20, "峰"), (23, "谷")]:
    ep = _dt.now().replace(hour=hh, minute=0, second=0, microsecond=0).timestamp()
    assert w._period_of(ep) == exp, (hh, w._period_of(ep))
print("period_of boundaries OK")

# 自定义时段边界验证
w.tou_valley = (0, 6)      # 0:00-6:00 谷
w.tou_peak = [(12, 14)]    # 12:00-14:00 峰
def hr_epoch(hh): return _dt.now().replace(hour=hh, minute=0, second=0, microsecond=0).timestamp()
assert w._period_of(hr_epoch(3)) == "谷"
assert w._period_of(hr_epoch(13)) == "峰"
assert w._period_of(hr_epoch(20)) == "平"
print("custom windows OK:", w._tou_window_desc())

# 功耗超阈值告警（测试中把通知打桩，避免弹窗阻塞）
w._notify = lambda *a, **k: None
w.model.calib_k = 1.0
w.model.calib_idle = 0.0
w.model.calib_peak = 0.0
w.running = True
w.last_ts = time.time()
w.alert_enabled = True
w.alert_threshold = 200.0
w._alert_active = False
w.on_sample({"cpu_load": 100, "gpu_power": 180, "gpu_valid": True})
print("alert high wall=", w.cur["wall"], "thr=", w.alert_threshold, "active=", w._alert_active)
assert w._alert_active is True, "wall>阈值 应触发告警"
w.on_sample({"cpu_load": 0, "gpu_power": 10, "gpu_valid": True})
print("alert low wall=", w.cur["wall"], "thr=", w.alert_threshold, "active=", w._alert_active)
assert w._alert_active is False, "wall<阈值 应复位"
print("alert OK (trigger+reset)")

# 方案对比计算（单一 vs 峰谷）
w.period_energy_wh = {"谷": 1000.0, "平": 2000.0, "峰": 500.0}
tou = (1.0 * w.rate_valley + 2.0 * w.rate_flat + 0.5 * w.rate_peak)
print("compare: single=%.3f tou=%.3f" % (w.energy_wh / 1000.0 * w.rate, tou))
assert abs(tou - 1.845) < 0.001

# 预估电费三档读数（每小时/每24小时/每月）
assert w.proj_hour.text().startswith("¥") and w.proj_day.text().startswith("¥") and w.proj_month.text().startswith("¥")
assert w.proj_hour.isWidgetType() and w.proj_hour.text() != "—"
print("projection:", w.proj_hour.text(), "/时 |", w.proj_day.text(), "/天 |", w.proj_month.text(), "/月")

# 阶梯电价
w.price_mode = "阶梯"
w.tier_base = 0.0
w.tier_l1, w.tier_r1 = 2160.0, 0.56
w.tier_l2, w.tier_r2 = 4800.0, 0.61
w.tier_r3 = 0.86
w.energy_wh = 1000.0 * 1000.0  # 本次 1000 kWh
assert abs(w._current_cost() - 1000 * 0.56) < 0.01, w._current_cost()
w.tier_base = 2000.0
exp = w._tiered_cost(3000.0) - w._tiered_cost(2000.0)
assert abs(w._current_cost() - exp) < 0.01, (w._current_cost(), exp)
print("tiered OK incr=", round(w._current_cost(), 2), "exp=", round(exp, 2))

# 历史会话归档验证
w.price_mode = "单一"
w.rate = 0.56
w.energy_wh = 3000.0          # 3 kWh
w.running_elapsed_ms = 2 * 3600 * 1000.0   # 2 小时
w.peak_wall = 250.0
w._archive_current()
hist = w._load_history()
print("history records =", len(hist))
assert len(hist) >= 1, "应至少归档 1 条"
assert abs(hist[-1]["kwh"] - 3.0) < 0.001, hist[-1]
assert hist[-1]["duration_h"] == 2.0
print("history archive OK:", hist[-1])
# 重置时若中途有累计应再归档一条
before = len(hist)
main.QMessageBox.information = lambda *a, **k: None   # 无界面打桩，避免弹窗阻塞
w.finished = False
w.reset_session()
assert len(w._load_history()) == before + 1, "reset 应再归档一条"
print("reset-archive OK, total=", len(w._load_history()))

# 历史导出验证（打桩文件对话框，校验 CSV/PNG 真写出）
main.QMessageBox.critical = lambda *a, **k: None
_tmp = os.path.dirname(os.path.abspath(__file__))
_csv = os.path.join(_tmp, "hist_test.csv")
main.QFileDialog.getSaveFileName = lambda *a, **k: (_csv, "CSV (*.csv)")
w._export_history_csv(w._load_history())
assert os.path.exists(_csv), "CSV 应写出"
with open(_csv, encoding="utf-8-sig") as f:
    _lines = f.read().splitlines()
assert len(_lines) >= 2, _lines
print("export csv OK, lines =", len(_lines), "|", _lines[0])

_png = os.path.join(_tmp, "hist_test.png")
main.QFileDialog.getSaveFileName = lambda *a, **k: (_png, "PNG (*.png)")
from PySide6.QtCharts import QChart, QChartView, QLineSeries as _LS
_ch = QChart(); _s = _LS(); _s.append(0, 1.0); _s.append(1, 2.0); _ch.addSeries(_s)
_vw = QChartView(_ch)
w._export_history_image(_vw)
assert os.path.exists(_png), "PNG 应写出"
print("export png OK, size =", os.path.getsize(_png), "bytes")
for _f in (_csv, _png):
    try: os.remove(_f)
    except Exception: pass

# 月度预算进度 + 超阈值预警
main.QMessageBox.warning = lambda *a, **k: None   # _notify 在 tray=None 时走 QMessageBox.warning，打桩防阻塞
w.price_mode = "单一"; w.rate = 0.56
w.energy_wh = 55.6
w.running_elapsed_ms = 3_600_000.0   # 1 小时 -> avg_w≈55.6W -> 月电费≈¥22.4
w.budget_kwh = 0.0
w.budget_alert_enabled = True
w.budget_alert_pct = 90.0
w.budget_cost = 20.0                  # 预算¥20，预计¥22.4 -> 超 90% 触发
w._refresh_readout()
# v18.32 主界面已移除「月度预算」卡片（budget_big/bar/sub 不复存在），
# 但预警通知逻辑保留：超 90% 应触发 _budget_alert_active
assert w._budget_alert_active is True, "应触发预算预警"
# 调高预算后 pct<90 应复位
w.budget_cost = 1000.0
w._refresh_readout()
assert w._budget_alert_active is False, "pct<阈值 应复位"
# 未设预算：预警状态复位即可（无 UI 断言）
w.budget_cost = 0.0; w.budget_kwh = 0.0
w._refresh_readout()
print("budget alert trigger/reset/unset OK")

# 待机占比统计（空闲帧 vs 活跃帧分类累加）
w.energy_wh = 0.0; w.idle_energy_wh = 0.0; w.active_energy_wh = 0.0
w.running = True; w.last_ts = time.time()
w.idle_cpu_thresh = 5.0; w.idle_gpu_thresh = 15.0
for _ in range(3):
    w.on_sample({"cpu_load": 1.0, "gpu_power": 5.0, "gpu_valid": True}); time.sleep(0.06)
for _ in range(3):
    w.on_sample({"cpu_load": 80.0, "gpu_power": 150.0, "gpu_valid": True}); time.sleep(0.06)
print("idle_wh =", round(w.idle_energy_wh, 4), "active_wh =", round(w.active_energy_wh, 4),
      "total =", round(w.energy_wh, 4))
assert w.idle_energy_wh > 0 and w.active_energy_wh > 0
assert abs(w.idle_energy_wh + w.active_energy_wh - w.energy_wh) < 1e-6, "idle+active 应等于总量"
idle_ratio = w.idle_energy_wh / w.energy_wh * 100.0
print("idle ratio =", round(idle_ratio, 1), "% | idle_big =", w.idle_big.text())
html = w._build_report_html()
assert "待机功耗分析" in html
print("idle report OK")

# 待机自动提醒（连续空闲超阈值弹通知，活跃后复位）
main.QMessageBox.warning = lambda *a, **k: None   # _notify 走 warning，打桩防阻塞
w.idle_nudge_enabled = True
w.idle_nudge_min = 1.0          # 1 分钟触发
w.idle_streak_ms = 0.0; w.idle_streak_energy_wh = 0.0; w._idle_nudged = False
w.running = True
for _ in range(2):
    w.last_ts = time.time() - 35  # 模拟距上次 35 秒
    w.on_sample({"cpu_load": 1.0, "gpu_power": 5.0, "gpu_valid": True})
print("idle_streak_ms =", round(w.idle_streak_ms, 1), "| nudged =", w._idle_nudged)
assert w._idle_nudged is True, "连续空闲超 1 分钟应弹提醒"
w.last_ts = time.time() - 0.1
w.on_sample({"cpu_load": 80.0, "gpu_power": 150.0, "gpu_valid": True})
assert w._idle_nudged is False, "活跃后应复位"
print("idle nudge OK (trigger+reset)")

# 节能情景模拟（核对计算逻辑）
w.price_mode = "单一"; w.rate = 0.56; w.psu_eff = 0.85
w.energy_wh = 1000.0 * 1000.0     # 1000 kWh
w.running_elapsed_ms = 3600 * 1000.0 * 1000.0   # 1000 小时 -> avg_w=1000W
r = w._simulate(8.0, 0.95)
print("sim avg_w =", round(r["avg_w"], 1), "| 关机省¥", round(r["sa"], 1), "| 电源省¥", round(r["sb"], 1))
assert r["avg_w"] == 1000.0, r["avg_w"]
assert r["sa"] > 0, "关机情景应有节省"
assert r["sb"] > 0, "电源升级应有节省"
# 情景1：1000W × 8h/天 × 30 = 240000 Wh = 240 kWh × 0.56 = 134.4
assert abs(r["sa_kwh"] - 240.0) < 0.01 and abs(r["sa"] - 134.4) < 0.01, (r["sa_kwh"], r["sa"])
# 情景2：月墙电=1000×720/1000=720kWh；系统=720×0.85=612；省墙=612×(1/0.85-1/0.95)=612×0.1242=76.0kWh ×0.56=42.6
assert abs(r["sb_kwh"] - 76.0) < 1.0 and abs(r["sb"] - 42.6) < 1.0, (r["sb_kwh"], r["sb"])
print("sim OK | total save ¥", round(r["sa"] + r["sb"], 1))

# 用电时段分布（按时钟小时聚合）
w.hourly = {
    "2026-09-03 08:00": [800.0, 2],   # 平均 400W
    "2026-09-03 20:00": [1000.0, 2],  # 平均 500W
    "2026-09-03 23:00": [200.0, 2],   # 平均 100W
}
hb = w._hourly_by_clock()
print("hourly_by_clock =", {k: round(v, 1) for k, v in hb.items()})
assert hb == {8: 400.0, 20: 500.0, 23: 100.0}, hb
# 峰谷着色所用的时段判定（先恢复默认峰谷窗口，避免被前面自定义窗口块影响）
w.tou_valley = (23, 7); w.tou_peak = [(8, 11), (18, 21)]
assert w._period_of(_dt(2026, 9, 3, 20, 0).timestamp()) == "峰"
assert w._period_of(_dt(2026, 9, 3, 23, 0).timestamp()) == "谷"
print("hourly OK")

# 电价快捷改价：峰谷模式下应写入「当前时段」对应的单价（stub 输入 0.44）
class _FakeDlg:
    @staticmethod
    def getDouble(*a, **k): return (0.44, True)
main.QInputDialog = _FakeDlg
w.price_mode = "峰谷"
w.tou_valley = (23, 7); w.tou_peak = [(8, 11), (18, 21)]
p_now = w._period_of(time.time())
before = {"谷": w.rate_valley, "平": w.rate_flat, "峰": w.rate_peak}
w.quick_edit_rate()
after = {"谷": w.rate_valley, "平": w.rate_flat, "峰": w.rate_peak}
for k in before:
    assert (after[k] == 0.44) if k == p_now else (after[k] == before[k]), (k, p_now, before, after)
print("quick rate OK | period =", p_now)
# 取消（ok=False）不应改动
main.QInputDialog.getDouble = staticmethod(lambda *a, **k: (9.9, False))
w.quick_edit_rate()
assert {"谷": w.rate_valley, "平": w.rate_flat, "峰": w.rate_peak} == after
print("quick rate cancel OK")

# 软件耗电分摊：按进程 CPU 时间增量占比分摊 CPU 估算功耗
w.app_cpu_snap = (0.0, {}); w.app_cpu_wh = {}
w.running = True; w.finished = False; w.last_ts = None
w.running_elapsed_ms = 0.0; w.window_hours = 720.0   # 防止触发 _finish 自动汇总弹窗
w.on_sample({"cpu_load": 100, "gpu_power": 100, "gpu_valid": True,
             "apps": (1000.0, {"chrome": 10.0, "code": 5.0})})   # 首帧：last_ts=None，仅建基线
time.sleep(0.06)
w.on_sample({"cpu_load": 100, "gpu_power": 100, "gpu_valid": True,
             "apps": (1002.1, {"chrome": 12.0, "code": 5.0})})   # 首个可对比帧：prev 为空仅推进快照
time.sleep(0.06)
w.on_sample({"cpu_load": 100, "gpu_power": 100, "gpu_valid": True,
             "apps": (1004.2, {"chrome": 14.0, "code": 5.0})})   # chrome +2s, code 不变
assert w.app_cpu_wh.get("chrome", 0.0) > 0, w.app_cpu_wh
assert "code" not in w.app_cpu_wh, w.app_cpu_wh            # 无增量不分摊
time.sleep(0.06)
w.on_sample({"cpu_load": 100, "gpu_power": 100, "gpu_valid": True,
             "apps": (1006.3, {"chrome": 15.0, "code": 6.0, "newproc": 9.0})})
assert "newproc" not in w.app_cpu_wh, w.app_cpu_wh          # 新进程不倒算历史
assert w.app_cpu_wh.get("code", 0.0) > 0, w.app_cpu_wh     # code 本轮有增量
html = w._build_report_html()
assert "软件耗电 Top10" in html
print("apps OK |", {k: round(v, 6) for k, v in w.app_cpu_wh.items()})

# v18.7 回归：设置集成到主界面右侧停靠面板（展开→改值→保存→生效并收起）
assert not w._settings_dock.isVisible()
w.btn_settings.click()
assert w._settings_dock.isVisible(), "点设置应展开面板"
assert abs(w._sw["rate"].value() - w.rate) < 1e-9, "展开时应同步当前生效值"
w._sw["rate"].setValue(0.66)
w._sw["samp"].setValue(3500)
w._save_settings_panel()
assert abs(w.rate - 0.66) < 1e-9, w.rate
assert w.sample_ms == 3500, w.sample_ms
assert w.worker._pending_interval == 3500, w.worker._pending_interval   # worker 线程安全应用
assert not w._settings_dock.isVisible(), "保存后应自动收起"
w.btn_settings.click()
assert abs(w._sw["rate"].value() - 0.66) < 1e-9, "再次展开应保留已存值"
w.btn_settings.click()
assert not w._settings_dock.isVisible()
print("settings panel OK (dock expand/save/sync)")

# v18 回归2：系统信息侧栏（AIDA64 风格）渲染
w._sys_static = {"os": "Microsoft Windows 10 家庭版", "osarch": "64位", "fw": "UEFI",
                 "sb": 1, "mb": "JGINYUE B450M-PLUS", "cpu": w.hw.cpu_name,
                 "cores": 6, "threads": 12, "mhz": 3700, "l1": 384, "l2": 2048, "l3": 32768,
                 "gpuname": w.hw.gpu_name, "gpuvram": 8589934592, "gpures": "2560", "gpuref": "119",
                 "disks": [{"model": "ST1000LM035-1RK174", "media": "HDD", "letters": "D:",
                            "sizeGB": 932},
                           {"model": "HS-SSD-C2000L", "media": "SSD", "letters": "C:",
                            "sizeGB": 256}],
                 "nic": "Realtek PCIe GbE Family Controller", "ip": "192.168.1.12"}
w._sys_dyn = {"ram_total": 34359738368, "ram_used": 17179869184, "ram_pct": 50.0,
              "mhz": 3700, "boot": time.time() - 3661, "down_kbs": 1.5, "up_kbs": 0.5,
              "cpu_temp": 45.0, "gpu_temp": 41.0}
w._update_sysinfo()
_html = w.sysinfo_view.toHtml()
# 注意：不要断言 '网　络' 标签本身——全角空格会被 toHtml() 转成 &#160;，匹配不到；
# 用网络块的稳定内容（IP 行）代替。
# v18.33：运行时间/操作系统上移到标题 QLabel（_hdr_uptime/_hdr_os），侧栏不再含这两行。
for key in ("处理器", "物理内存", "显卡", "磁盘0", "192.168.1.12"):
    assert key in _html, key
assert "启动模式" not in _html, "v18.35: 启动模式已按用户要求删除"
assert "运行时间" not in _html and "操作系统" not in _html, "v18.33: 这两项应已上移标题"
assert "运行" in w._hdr_uptime.text() and w._hdr_os.text(), "v18.33: 标题运行时间/OS 标签"
print("sysinfo panel OK")

# v18 回归3：始终监测语义（无 btn_start；reset 后仍 running）
assert not hasattr(w, "btn_start")
w.reset_session()
assert w.running is True and w.finished is False
print("always-on monitor OK")

# v18.4 回归：set_interval 跨线程安全（UI 线程只排队，worker 线程应用）
assert w.worker.interval != 3000
w.worker.set_interval(3000)          # 模拟设置保存（UI 线程调用）
assert w.worker._pending_interval == 3000
assert w.worker.interval != 3000     # 未被跨线程直接改
time.sleep(4.2)                       # 等 worker 下一 tick 自行应用（间隔可达 3.5s，3.2 会偶发不够）
assert w.worker.interval == 3000, w.worker.interval
assert w.worker._pending_interval is None
print("set_interval thread-safe OK")

# v18.6 回归1：单实例保护（跨进程命名互斥体）
_mn = "PC用电电费计算器_SingleInstance"
import subprocess as _sp
_holder = _sp.Popen([sys.executable, "-c",
    "import ctypes,time; ctypes.windll.kernel32.CreateMutexW(None,False,%r); time.sleep(20)" % _mn])
time.sleep(1.2)   # 等子进程完成互斥体创建
assert main._acquire_single_instance(_mn) is False, "他进程持锁时应返回 False"
assert main._acquire_single_instance(_mn + "_probe_unique") is True, "空闲锁名应返回 True"
_holder.kill(); _holder.wait()
print("single-instance OK (held->False, free->True)")

# v18.13 回归：心跳新鲜 ≠ 持有者还活着（45 秒内重启不得被自己拦在门外）
_mn2 = _mn + "_restart_probe"
_lk = main.LOCK_FILE
_holder2 = _sp.Popen([sys.executable, "-c",
    "import ctypes,time; ctypes.windll.kernel32.CreateMutexW(None,False,%r); time.sleep(25)" % _mn2])
time.sleep(1.2)
# ① 心跳新鲜 + 持有者存活 → 拒绝启动
with open(_lk, "w", encoding="utf-8") as _f:
    _f.write("%.3f %d" % (time.time(), _holder2.pid))
assert main._acquire_single_instance(_mn2) is False, "心跳新鲜且持有者存活 → 应拒绝"
# ② 心跳新鲜但持有者已死（刚关掉就重启，v18.12 会误拦）→ 必须放行
_dead = _sp.Popen([sys.executable, "-c", "pass"]); _dead.wait()
with open(_lk, "w", encoding="utf-8") as _f:
    _f.write("%.3f %d" % (time.time(), _dead.pid))
assert main._acquire_single_instance(_mn2) is True, "心跳新鲜但持有者已死 → 应接管"
# ③ 旧格式心跳（只有时间戳、无 PID）→ 兼容放行
with open(_lk, "w", encoding="utf-8") as _f:
    _f.write("%.3f" % time.time())
assert main._acquire_single_instance(_mn2) is True, "旧格式心跳应兼容放行"
# ④ _pid_alive 基本正确性
assert main._pid_alive(os.getpid()) is True, "自身 PID 应判定存活"
assert main._pid_alive(_dead.pid) is False, "已退出进程应判定死亡"
assert main._pid_alive(0) is False, "PID 0 应判定无效"
# ⑤ _beat 写入的必须是「时间戳 + PID」两段
main._beat()
with open(_lk, "r", encoding="utf-8") as _f:
    _parts = _f.read().split()
assert len(_parts) == 2 and int(_parts[1]) == os.getpid(), _parts
_holder2.kill(); _holder2.wait()
print("restart-within-45s OK (dead owner -> take over)")

# v18.6 回归2：托盘悬停实时功率（offscreen 无托盘，用桩验证 tooltip 内容）
class _FakeTray:
    def __init__(self): self._tt = ""
    def setToolTip(self, s): self._tt = s
    def toolTip(self): return self._tt
_tray_bak = getattr(w, "tray", None)
w.tray = _FakeTray()
w.running = True; w.last_ts = time.time()
w.on_sample({"cpu_load": 30.0, "gpu_power": 60.0, "gpu_valid": True})
_tt = w.tray.toolTip()
assert "当前功率" in _tt and "W" in _tt and "kWh" in _tt, _tt
print("tray tooltip OK |", _tt.replace("\n", " / "))
w.tray = _tray_bak

# v18.8 回归1：托盘功率图标 pixmap 生成
_pm = w._tray_icon_pixmap(234.0)
assert not _pm.isNull() and _pm.width() == 64, "托盘图标应生成 64px pixmap"
print("tray watts icon OK")

# v18.8 回归2：迷你悬浮窗开关 / 内容刷新 / 会话持久化
w._toggle_mini(True)
assert w._mini_visible is True and w.mini.isVisible(), "应显示悬浮窗"
w._update_mini()
assert "W" in w.mini.lbl_w.text() and "kWh" in w.mini.lbl_cost.text(), \
    (w.mini.lbl_w.text(), w.mini.lbl_cost.text())
w.mini.move(120, 88)                       # 模拟拖动
w._toggle_mini(False)
assert w._mini_visible is False and not w.mini.isVisible()
assert w._mini_pos == [120, 88], w._mini_pos   # 位置已记住
w._toggle_mini(True)
assert w.mini.x() == 120 and w.mini.y() == 88  # 重开恢复位置
w._toggle_mini(False)
w._save_session()
import json as _json
with open(main.SESSION_FILE, encoding="utf-8") as f:
    _sd = _json.load(f)
assert "mini_visible" in _sd and "mini_pos" in _sd, list(_sd.keys())
print("mini overlay OK | pos =", _sd["mini_pos"], "| visible =", _sd["mini_visible"])

# ---- v18.13 悬浮窗：嵌入桌面 / 背景全透明 / 取消双击隐藏 ----
from PySide6.QtWidgets import QWidget as _QW
from PySide6.QtCore import Qt as _Qt, QPointF as _QF
assert main.MiniOverlay.paintEvent is _QW.paintEvent, \
    "背景要全透明，不应再重写 paintEvent 自绘圆角底板"
assert main.MiniOverlay.mouseDoubleClickEvent is _QW.mouseDoubleClickEvent, \
    "双击隐藏必须取消：拖动时误触会把窗口弄丢"
for _n in ("lbl_w", "lbl_sub", "lbl_cost"):
    _eff = getattr(w.mini, _n).graphicsEffect()
    assert _eff is not None and _eff.blurRadius() >= 4 and _eff.offset() == _QF(0, 0), \
        "%s 需要黑色描边，否则浅色壁纸上白字看不见" % _n
# v18.15 层级：默认嵌入桌面（置底），切置顶后标志必须二选一、且始终保持可见
assert w.mini.layer_on_top() is False, "默认应为嵌入桌面（不遮挡任何窗口）"
_BOT = _Qt.WindowType.WindowStaysOnBottomHint
_TOP = _Qt.WindowType.WindowStaysOnTopHint
assert (w.mini.windowFlags() & _BOT), "嵌入桌面靠 WindowStaysOnBottomHint 实现"
assert not (w.mini.windowFlags() & _TOP), "嵌入桌面时不能同时带置顶标志"
w.mini.show()
assert w.mini.isVisible(), "嵌入桌面模式下悬浮窗也必须可见"

_got = []
w.mini._layer_cb = lambda v: _got.append(v)
w.mini.set_layer(True)
assert w.mini.layer_on_top() is True, "切置顶后 layer_on_top 应为 True"
assert (w.mini.windowFlags() & _TOP), "切置顶后应有置顶标志"
assert not (w.mini.windowFlags() & _BOT), "置顶与置底不能同时存在"
assert w.mini.isVisible(), "切层级后仍应保持可见（不能把窗口弄丢）"
assert _got == [True], ("层级回调应被触发一次", _got)
w.mini.set_layer(False)
assert w.mini.layer_on_top() is False
assert (w.mini.windowFlags() & _BOT) and not (w.mini.windowFlags() & _TOP)
assert _got == [True, False], _got

# SetParent 挂桌面已废弃：分层子窗口在 Windows 上不渲染，会让悬浮窗彻底隐身
assert not hasattr(w.mini, "_dock_to_desktop"), "SetParent 挂桌面方案已废弃"
w.mini._layer_cb = None
w.mini.hide()
print("mini overlay v18.15 OK | 无底板 无双击隐藏 | 层级可切(置底/置顶) 切换后仍可见")

# ---- v18.18 悬浮窗：全面功耗构成（全部项，自动换行，高度自适应） ----
_m = w.mini
def _mini_bd_rows(_m):
    """读取当前票据式构成表的所有行。v18.22 起每行是一个 QWidget(QHBoxLayout)；
    v18.36 四列：0=部件名 1=使用率 2=瓦数 3=温度/转速。"""
    rows = []
    for _row in _m._bd_row_widgets:
        _lay = _row.layout()
        _ln = _lay.itemAt(0).widget().text()
        _lv = _lay.itemAt(2).widget().text()
        rows.append((_ln, _lv))
    return rows

assert _m.bd_visible() is True, "默认应显示功耗构成"
# v18.36 先灌 12 行数据：行数少时总高会被 setFixedHeight(max(120,...)) 钳住，
# 开/关构成的高度差就看不出来（offscreen 下基础块比真实屏矮）
_m.set_breakdown({f"部件{i:02d}": 10.0 + i for i in range(12)})
app.processEvents()
_m.set_bd_visible(False)
_h0 = _m.height()          # 不显示构成时的基准高度
_m.set_bd_visible(True)
_h1 = _m.height()
assert _h1 > _h0, (_h0, _h1)
_m.set_breakdown({"CPU": 45.2, "GPU": 82.7, "显示器": 30.0,
                  "主板": 18.4, "内存": 6.1, "HDD": 8.5, "SSD": 3.2},
                 {"CPU": "12%", "GPU": "55%", "显示器": "—", "主板": "—",
                  "内存": "63%", "HDD": "—", "SSD": "—"},
                 {"CPU": "76°", "GPU": "42°", "显示器": "—", "主板": "28°",
                  "内存": "—", "HDD": "—", "SSD": "—"})
_rows = _mini_bd_rows(_m)
_names = [r[0] for r in _rows]
for _frag in ("GPU", "CPU", "显示器", "主板", "内存", "HDD", "SSD"):
    assert _frag in _names, (_frag, _names)
assert "其他" not in _names, "v18.18 起全部列出，不应再归并「其他」: %s" % _names
assert _names[-1] == "合计", _names
assert all(r[1].endswith(" W") for r in _rows), _rows
# v18.36 四列抽查：GPU 行 = ('GPU', '82 W')，使用率/温度列存在且非空
_r0 = _m._bd_row_widgets[0].layout()
_cells = [_r0.itemAt(i).widget().text() for i in range(_r0.count())]
assert len(_cells) == 4 and _cells[0] == "GPU" and _cells[1] == "55%" \
    and _cells[2] == "83 W" and _cells[3] == "42°", _cells
app.processEvents()
assert _m.height() > _h0, "7 项构成后高度应大于基准: %s vs %s" % (_m.height(), _h0)
assert _m.width() == 320, _m.width()  # v18.36 起悬浮窗宽 240→320（构成行四列）
# 关掉：控件隐藏 + 高度回到基准；再打开恢复显示
_m.set_bd_visible(False)
assert _m.bd_visible() is False and not _m.bd_widget.isVisible()
_m.set_bd_visible(True)
assert _m.bd_visible() is True
# 空数据 / None 都不该崩，且清空行
_m.set_breakdown({})
assert _mini_bd_rows(_m) == [], "空构成不应有行"
_m.set_breakdown(None)
assert _mini_bd_rows(_m) == [], "None 不应有行"
print("mini breakdown full OK | 高度 %d -> %d（全量 %d 项）" % (_h0, _m.height(), 7))

# ---- v18.17 主界面宽度：此前被内容顶到 1983px，resize() 形同虚设 ----
w.resize(1120, 820)
app.processEvents()
_mw = w.minimumSizeHint().width()
w.resize(1080, 880)
app.processEvents()
_mw = w.minimumSizeHint().width()
assert w.width() <= 1100, "实际宽度 %d px 没按 resize 生效" % w.width()
print("main window width OK | minimumHint=%d 实际=%d（修复前 1983）" % (_mw, w.width()))

# v18.9 回归：每日日报（小时桶汇总 → HTML 落盘）+ 跨天自动触发
w.price_mode = "单一"; w.rate = 0.56
w.hourly["2026-09-02 08:00"] = [400.0 * 2, 2]   # 均 400W → 0.4 kWh
w.hourly["2026-09-02 20:00"] = [500.0 * 3, 3]   # 均 500W → 0.5 kWh
_p = w._daily_report("2026-09-02")
assert _p and os.path.exists(_p), "日报文件应写出"
with open(_p, encoding="utf-8") as f:
    _c = f.read()
assert "2026-09-02" in _c and "0.90" in _c and "¥0.50" in _c, "日报应含 0.9kWh / ¥0.50"
assert "功率峰值 500 W（20 点时段）" in _c, "应标出峰值时段"
os.remove(_p)
# 跨天翻转：把锚点改成昨天，喂一帧采样应自动生成昨日日报并推进锚点
w._last_date = "2026-09-02"
w.running = True; w.last_ts = time.time()
w.on_sample({"cpu_load": 30.0, "gpu_power": 60.0, "gpu_valid": True})
assert w._last_date == time.strftime("%Y-%m-%d"), w._last_date
_p2 = os.path.join(main.BASE_DIR, "用电日报_2026-09-02.html")
if os.path.exists(_p2):
    os.remove(_p2)   # 自动触发产物，清理
print("daily report OK (html + rollover)")

# v18.10 回归：设置浮层 ✕ 收起 + 迷你悬浮窗复选框联动 + Esc 收起
w.btn_settings.click()
assert w._settings_dock.isVisible()
w._sw["mini"].setChecked(True)
w._save_settings_panel()
assert w._mini_visible is True and w.mini.isVisible(), "保存后应联动显示悬浮窗"
w.btn_settings.click()
assert w._settings_dock.isVisible()
# 标题栏 ✕ 按钮（面板第一个 QPushButton，文本 ✕）
btn_x = None
for b in w._settings_dock.findChildren(main.QPushButton):
    if b.text() == "✕":
        btn_x = b; break
assert btn_x is not None, "应存在 ✕ 收起按钮"
btn_x.click()
assert not w._settings_dock.isVisible(), "✕ 应收起浮层"
# Esc 收起
w.btn_settings.click()
assert w._settings_dock.isVisible()
from PySide6.QtCore import QEvent as _QEv
from PySide6.QtGui import QKeyEvent as _QKeyEv
w.keyPressEvent(_QKeyEv(_QEv.Type.KeyPress, main.Qt.Key.Key_Escape,
                        main.Qt.KeyboardModifier.NoModifier))
assert not w._settings_dock.isVisible(), "Esc 应收起浮层"
# 复选框联动关闭
w._sw["mini"].setChecked(False)   # 浮层已收，直接走 toggle 验证同步
w._toggle_mini(False)
assert w._mini_visible is False
print("settings drawer UX OK (x-btn / esc / mini sync)")


# ---- v18.11 动态电源效率曲线 ----
E = main.PM.psu_eff_at
assert E(100, 0) is None and E(100, -1) is None and E(100, None) is None, \
    "未设额定功率必须返回 None（沿用固定效率，行为不变）"
_e5, _e20, _e50, _e100 = E(0.05 * 500, 500), E(0.20 * 500, 500), E(0.50 * 500, 500), E(500, 500)
assert 0.69 <= _e5 <= 0.71, _e5
assert _e50 > _e20 > _e5, (_e5, _e20, _e50)          # 轻载效率低，50% 附近最高
assert _e50 > _e100, (_e50, _e100)                    # 满载反而回落
assert E(2000, 500) == _e100, "超额定应截断在满载点"
print("psu_eff_at curve OK | 5%%=%.3f 20%%=%.3f 50%%=%.3f 100%%=%.3f" % (_e5, _e20, _e50, _e100))

# 未设额定功率 -> 效率恒等于固定值（向后兼容）
w.model.psu_rating_w = 0.0
_a = main.PM.estimate(w.model, 0, None, False, True)
_b = main.PM.estimate(w.model, 100, 180, True, True)
assert _a["psu_eff"] == round(w.model.psu_efficiency, 4) == _b["psu_eff"], \
    (_a["psu_eff"], _b["psu_eff"])

# 设了额定功率 -> 轻载/重载效率不同，且插墙功耗随之变化
w.model.psu_rating_w = 500.0
_lo = main.PM.estimate(w.model, 0, None, False, True)
_hi = main.PM.estimate(w.model, 100, 180, True, True)
assert _lo["psu_eff"] != _hi["psu_eff"], (_lo["psu_eff"], _hi["psu_eff"])
assert abs(_lo["psu_eff"] - E(_lo["calib_sys_watts"] - _mon, 500.0)) < 1e-3
_dc_hi = _hi["calib_sys_watts"] - _mon
assert abs(_hi["wall_watts"] - (_dc_hi / _hi["psu_eff"] + _mon)) < 0.2
print("dynamic psu eff OK | idle eff=%.3f (%.0fW) peak eff=%.3f (%.0fW)"
      % (_lo["psu_eff"], _lo["calib_sys_watts"] - _mon,
         _hi["psu_eff"], _dc_hi))
w.model.psu_rating_w = 0.0

# 设置面板 → 保存 → 会话持久化往返
w.open_settings()
w._sw["pr"].setValue(650)
w._save_settings_panel()
assert w.psu_rating_w == 650.0 and w.model.psu_rating_w == 650.0, \
    (w.psu_rating_w, w.model.psu_rating_w)
w._save_session()
w.psu_rating_w = 0.0; w.model.psu_rating_w = 0.0
w._load_session_maybe()
assert w.psu_rating_w == 650.0 and w.model.psu_rating_w == 650.0, \
    "额定功率应随会话持久化恢复"
w.open_settings()
assert w._sw["pr"].value() == 650, w._sw["pr"].value()   # 面板回显
print("psu rating roundtrip OK (panel -> session -> panel) =", w.psu_rating_w)

# 读数区显示「（动态）」标记 + 实时效率
w.psu_rating_w = 650.0; w.model.psu_rating_w = 650.0
_est = main.PM.estimate(w.model, 40, 47.0, True, True)
w.cur = {"cpu_load": 40.0, "gpu_power": 47.0, "gpu_valid": True,
         "wall": _est["wall_watts"], "sys": _est["sys_watts"],
         "breakdown": _est["breakdown"], "display_on": True,
         "psu_eff": _est["psu_eff"]}
w._refresh_readout()
assert "动态" in w.wall_sub.text(), w.wall_sub.text()
assert ("%.2f" % w.cur["psu_eff"]) in w.wall_sub.text(), w.wall_sub.text()
_txt_dyn = w.wall_sub.text()
w.psu_rating_w = 0.0; w.model.psu_rating_w = 0.0
w._refresh_readout()
assert "动态" not in w.wall_sub.text(), w.wall_sub.text()
print("readout dynamic-eff badge OK\n  动态:", _txt_dyn, "\n  固定:", w.wall_sub.text())

# v18.13 读数行标签：直流 / 损耗 / 插座 三个数必须自洽，且与 cur 一致
# （旧文案把直流值标成「含电源损耗」，看起来像开关屏数值反了）
w.psu_rating_w = 650.0; w.model.psu_rating_w = 650.0
for _don in (True, False):
    _e = main.PM.estimate(w.model, 45, 47.0, True, _don)
    w.cur = {"cpu_load": 45.0, "gpu_power": 47.0, "gpu_valid": True,
             "wall": _e["wall_watts"], "sys": _e["sys_watts"],
             "breakdown": _e["breakdown"], "display_on": _don,
             "psu_eff": _e["psu_eff"]}
    w.display_on = _don
    w._refresh_readout()
    _t = w.wall_sub.text()
    # v18.32 新文案：'插座 X W ＝ 直流 Y + 损耗 Z · 效率 E'
    _dc = float(_t.split("直流 ")[1].split(" +")[0])
    _ls = float(_t.split("损耗 ")[1].split(" ·")[0])
    _so = float(_t.split("插座 ")[1].split(" W")[0])
    assert abs(_dc - w.cur["sys"]) < 0.05, (_t, w.cur["sys"])
    assert abs(_so - w.cur["wall"]) < 0.05, (_t, w.cur["wall"])
    assert abs((_dc + _ls) - _so) < 0.05, "直流 + 损耗 必须等于插座: " + _t
    assert _ls > 0, "含电源损耗时损耗必须为正: " + _t
    if _don:
        assert "显示器 +" in _t, "开屏应显式标注状态: " + _t
    else:
        assert "显示器关" in _t, "关屏应显式标注状态: " + _t
    print("  屏幕%s: %s" % ("开" if _don else "关", _t))
w.display_on = True
print("readout label OK (直流 + 损耗 = 插座，开关屏状态均标注)")

# 电源建议应只按主机峰值（不含显示器），且用峰值点效率
w.psu_rating_w = 650.0; w.model.psu_rating_w = 650.0
w._refresh_readout()
assert "主机峰值" in w.psu_hint.text() and "效率" in w.psu_hint.text(), w.psu_hint.text()
print("psu hint OK |", w.psu_hint.text())


# ---- v18.13 电源额定功率「按推荐填入」----
from PySide6.QtWidgets import QPushButton as _QPBtn
_sug = w._suggest_psu_rating()
assert _sug in main.PSU_COMMON, "推荐值必须落在常见档位里: %r" % (_sug,)
_host_pk = float(w.model.peak_sys) - float(w.model.monitor_w)
_need = _host_pk / 0.88 * 1.3
assert abs(_sug - _need) <= 75, "应取最接近的档位: 建议 %r vs 需求 %.1f" % (_sug, _need)
assert _host_pk <= _sug, "推荐额定功率必须 >= 主机峰值，否则负载率会超过 100%"
w.open_settings()
_prw = w._sw["pr"].parentWidget()
_btns = _prw.findChildren(_QPBtn)
assert len(_btns) == 1, "额定功率行应有且只有一个按钮: %r" % (_btns,)
w._sw["pr"].setValue(0)
_btns[0].click()          # 真实点击
assert w._sw["pr"].value() == _sug, (w._sw["pr"].value(), _sug)
print("psu rating suggestion OK | 主机峰值 %.0fW -> 推荐 %dW (点击已写入)" % (_host_pk, _sug))

# ---- v18.13 全屏豁免：看视频不得被误判成熄屏 ----
_f_orig = main.H.foreground_fullscreen, main.H.user_idle_sec
try:
    main.H.user_idle_sec = lambda: 600.0          # 远超 300s 息屏超时
    main.H.foreground_fullscreen = lambda: True   # 全屏放映/看片中
    assert main.H.display_auto_off() is False, \
        "有真全屏窗口时必须判定屏幕亮着，否则整场电影都会被少算显示器功耗"
    main.H.foreground_fullscreen = lambda: False
    assert main.H.display_auto_off() is True, "空闲超时且无全屏 → 应判定已熄屏"
    # 联动 _display_on：自动模式下全屏即视为亮屏
    w._disp_manual = None
    main.H.foreground_fullscreen = lambda: True
    assert w._display_on() is True, "自动模式 + 全屏 → 屏幕亮"
    main.H.foreground_fullscreen = lambda: False
    assert w._display_on() is False, "自动模式 + 久空闲 → 屏幕灭"
    # 手动标记优先于全屏推断
    w._disp_manual = False
    main.H.foreground_fullscreen = lambda: True
    assert w._display_on() is False, "用户手动标记息屏时应以手动为准"
    # v18.14 生效期内（刚设完）即使有键鼠操作也不被自动唤醒抵消
    # —— 否则用户选「记为关闭」看不到数值下降，会以为开关屏逻辑反了
    main.H.user_idle_sec = lambda: 0.5          # 刚点完菜单，空闲≈0
    w._disp_manual_ts = time.time()             # 刚设
    assert w._display_on() is False, "生效期内有操作也必须保持「关闭」"
    w._disp_manual_ts = time.time() - (main.DISP_MANUAL_GRACE_SEC + 60)
    assert w._display_on() is True, "生效期过后有操作应自动恢复「开启」"
    main.H.user_idle_sec = lambda: 600.0        # 还原为久空闲
    assert w._display_on() is False, "生效期过后仍久空闲 → 维持关闭"
    print("fullscreen exemption OK (video -> screen on, idle -> off, manual wins + grace)")
finally:
    main.H.foreground_fullscreen, main.H.user_idle_sec = _f_orig
    w._disp_manual = None

# ---- v18.12 硬件重检测（增减硬盘/显示器后重建模型）----
from PySide6.QtWidgets import QMessageBox
_msgs = []
_orig_info = QMessageBox.information
QMessageBox.information = staticmethod(lambda *a, **k: _msgs.append(str(a[2]) if len(a) > 2 else ""))
try:
    import copy as _copy
    _hw2 = _copy.deepcopy(w.hw)
    _hdd_before = w.model.components.get("HDD", 0.0)
    _hw2.disks = list(_hw2.disks) + [("HDD", 1000)]      # 模拟新插一块机械盘
    _mon_before = w.model.monitor_w
    _hw2.monitor_count = max(1, _hw2.monitor_count) + 1  # 再接一台显示器
    main.H.detect_hardware = lambda: _hw2
    main.H.collect_system_info = lambda: dict(w._sys_static or {})
    # 预先埋入校准与电源设置，验证重检测不会把它们冲掉
    w.model.calib_idle = 80.0; w.model.calib_peak = 400.0; w.model.calib_k = 1.0
    w.psu_rating_w = 650.0; w.model.psu_rating_w = 650.0
    w._redetect_hardware()
    assert w.model.components["HDD"] > _hdd_before + 0.05, \
        (w.model.components["HDD"], _hdd_before)
    assert w.model.monitor_w > _mon_before + 0.05, (w.model.monitor_w, _mon_before)
    assert w.model.calib_idle == 80.0 and w.model.calib_peak == 400.0, \
        "校准数据应保留"
    assert w.model.psu_rating_w == 650.0 and w.model.psu_efficiency == w.psu_eff, \
        "电源设置应保留"
    assert _msgs, "重检测后应弹差异提示"
    assert "HDD" in _msgs[-1] and "显示器" in _msgs[-1], _msgs[-1]
    print("hardware re-detect OK | HDD %.1f->%.1f W, 显示器 %.1f->%.1f W"
          % (_hdd_before, w.model.components["HDD"], _mon_before, w.model.monitor_w))
    # 无变化时不应报差异
    _msgs.clear()
    w._redetect_hardware()
    assert "未检测到硬件变化" in _msgs[-1], _msgs[-1]
    print("re-detect no-change OK")
finally:
    QMessageBox.information = _orig_info
    w.model.calib_idle = 0.0; w.model.calib_peak = 0.0
    w.psu_rating_w = 0.0; w.model.psu_rating_w = 0.0

# ---- v18.16 设置抽屉不得横向裁切：内容最小宽度必须装得进可视区 ----
# 之前的坑：长标签把 QFormLayout 的标签列撑到 228px，加上字段列共 525px，
# 而抽屉可视区只有 470px —— 右侧内容直接被裁掉，表现为「设置显示不全」。
w.resize(1080, 760)
w._reposition_settings_dock()
w._settings_dock.show()
app.processEvents()
_avail = w._settings_dock.viewport().width()
_minw = w._settings_dock.widget().minimumSizeHint().width()
assert _minw <= _avail, (
    "设置内容最小宽度 %d px > 可视区 %d px，右侧会被裁掉" % (_minw, _avail))
w._settings_dock.hide()
print("settings dock no-clip OK | 内容最小 %d px <= 可视区 %d px" % (_minw, _avail))

# ---- v18.26 报告导出 PNG + 迷你悬浮窗快照 ----
_html = w._build_report_html()
assert "迷你悬浮窗" in _html, "报告里应包含迷你悬浮窗快照"
assert "瞬时插座功耗" in _html, "快照应含瞬时功耗行"
assert "本轮累计" in _html, "快照应含本轮累计行"
_png = os.path.join(_TMPDIR, "report_test.png")
if os.path.exists(_png):
    os.remove(_png)
assert w._render_png(_html, _png) is True, "PNG 渲染应返回 True"
assert os.path.exists(_png), "PNG 文件应生成"
_size = os.path.getsize(_png)
assert _size > 1024, "PNG 不应只有 %d 字节" % _size
with open(_png, "rb") as _f:
    assert _f.read(8) == b"\x89PNG\r\n\x1a\n", "文件头应是 PNG 签名"
print("report PNG OK | %d bytes, 含迷你悬浮窗快照" % _size)
# 导出入口应指向 PNG
assert callable(w.export_report) and callable(w._save_png)
assert hasattr(w, "_render_png")
print("export_report -> PNG OK")

w.close()
print("HEADLESS_OK")
