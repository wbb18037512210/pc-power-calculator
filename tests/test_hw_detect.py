# -*- coding: utf-8 -*-
"""v18.31 硬件识别增强回归测试（P0 虚拟显卡过滤 / P1a 笔记本判定 / P1b 显示器建模）。

这些点都是「静态读代码看不出、必须在真机上跑才知道」的坑，一旦退化会静默
把功耗算错（虚拟显卡被当主显卡 → 180W 变 75W），所以锁死在这里。

运行：python tests/test_hw_detect.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hardware as H
import power_model as PM

PASS, FAIL = [], []


def check(name, got, want):
    if got == want:
        PASS.append(name)
    else:
        FAIL.append(f"{name}: got={got!r} want={want!r}")


# ---------------------------------------------------------------- P0 虚拟显卡
print("--- P0 虚拟/伪显卡过滤 ---")
# 必须被过滤掉的（真实世界出现过的名字）
for name in ["GameViewer Virtual Display Adapter",   # 本机真实存在
             "GameViewer Display Adapter",           # 不带 Virtual，旧规则漏它
             "Idd Desk Adapter",
             "DDA Wrapper",
             "Microsoft Basic Render Driver",
             "Microsoft Remote Display Adapter",
             "Virtual Display",
             "Honor Virtual Display",
             "远程虚拟显示器"]:
    check(f"过滤 {name}", H._is_virtual_gpu(name), True)

# 必须保留的真实显卡（防误伤：名字里含 Microsoft/Basic 子串的真卡很罕见，
# 但要确保常见卡名不被规则误杀）
for name in ["NVIDIA GeForce GTX 1080", "NVIDIA GeForce RTX 3050",
             "AMD Radeon RX 6600", "Intel(R) UHD Graphics 770",
             "Intel(R) Arc(TM) A770 Graphics", "NVIDIA GeForce RTX 4060 Ti"]:
    check(f"保留 {name}", H._is_virtual_gpu(name), False)

# 端到端：虚拟卡排在真卡前面时，必须选中真卡
from hardware_id_v2 import identify_gpu
_VIRTUAL_FIRST = [
    {"Name": "GameViewer Display Adapter", "AdapterRAM": 0},
    {"Name": "NVIDIA GeForce GTX 1080", "AdapterRAM": 4293918720},
]
picked = None
for g in _VIRTUAL_FIRST:
    n = (g.get("Name") or "").strip()
    if not n or H._is_virtual_gpu(n):
        continue
    picked = n
    break
check("虚拟卡占位时仍选中真卡", picked, "NVIDIA GeForce GTX 1080")
_w, _conf, _ = identify_gpu(picked, 8.0)
check("真卡 TDP 未被虚拟卡污染", (_w, _conf), (180.0, "high"))


# ------------------------------------------------------------ P1a 笔记本判定
print("--- P1a 便携机判定 ---")
# 机箱类型为准
check("Chassis 9(Laptop)", H._detect_laptop({9}, "", False), True)
check("Chassis 10(Notebook)", H._detect_laptop({10}, "", False), True)
check("Chassis 32(Detachable)", H._detect_laptop({32}, "", False), True)
check("Chassis 3(Desktop)", H._detect_laptop({3}, "", False), False)
# 关键回归：台式机 + UPS（有电池）必须仍判为台式机
check("台式机+UPS(有电池)仍判台式", H._detect_laptop({3}, "OptiPlex 7090", True), False)
# 无机箱类型时才看电池
check("无Chassis+有电池→便携", H._detect_laptop(set(), "ThinkPad T14", True), True)
check("无Chassis+无电池→台式", H._detect_laptop(set(), "OptiPlex", False), False)
# 虚拟机排除
check("虚拟机(VMware)不算便携", H._detect_laptop(set(), "VMware Virtual Platform", True), False)
check("云主机(KVM)不算便携", H._detect_laptop(set(), "KVM Server", True), False)


# ------------------------------------------------------------ P1b 显示器建模
print("--- P1b 显示器功耗建模 ---")
check("无EDID返回None(触发回退)", PM.monitor_watts(None, 0, 0, 1440), None)
check("无EDID无英寸返回None", PM.monitor_watts(0, 0, 0, 0), None)
# 标定校验（标定值来自 24.5" 1440p = 30.0W，与旧常量 monitor_1440p 一致）
for label, inch, vpx, lo, hi in [
        ("15.6\"笔记本屏", 15.6, 1080, 6.0, 12.0),
        ("23.8\" 1080p", 23.8, 1080, 18.0, 25.0),
        ("24.5\" 1440p", 24.5, 1440, 28.0, 33.0),
        ("27\" 1440p", 27.0, 1440, 32.0, 40.0),
        ("32\" 4K", 32.0, 2160, 42.0, 55.0)]:
    w = PM.monitor_watts(inch, 0, 0, vpx)
    ok = lo <= w <= hi
    check(f"{label} 在 {lo}-{hi}W", ok, True)

# 用实测宽高时优先走面积（本机 54x31cm）
w_by_size = PM.monitor_watts(24.5, 54.0, 31.0, 1440)
check("实测宽高优先(54x31cm)", round(w_by_size, 1), 30.3)
# 钳制边界
check("超大屏钳制上限", PM.monitor_watts(120, 0, 0, 2160), PM.MONITOR_W_MAX)
check("极小屏钳制下限", PM.monitor_watts(2, 0, 0, 480), PM.MONITOR_W_MIN)
# PnP 码提取
check("PnP码: DISPLAY\\SGT2450\\...",
      H._extract_monitor_pnp(r"DISPLAY\SGT2450\5&264d596b&0&UID41222_0"), "SGT2450")
check("PnP码: # 分隔符归一化",
      H._extract_monitor_pnp("DISPLAY#SGT2450#5&264d596b&0"), "SGT2450")
check("PnP码: 空输入", H._extract_monitor_pnp(""), "")


# ------------------------------------------------- 集成：build_model 回归保护
print("--- 集成：build_model ---")
class _FakeHW:
    cpu_name = "AMD Ryzen 5 5600X 6-Core Processor"
    gpu_name = "NVIDIA GeForce GTX 1080"
    gpu_vram_bytes = 8 * 10**9
    gpu_resolution = "2560x1440"
    gpu_is_nvidia = True
    ram_bytes = 34 * 10**9
    disks = [("SSD", 1000.0), ("HDD", 2000.0), ("HDD", 4000.0)]
    monitor_count = 1
    has_battery = False
    is_laptop = False
    monitors = []
    raw = {}


# 无 monitors（老数据/EDID 取不到）→ 必须完全回退到旧三档，结果不变
hw0 = _FakeHW()
m0 = PM.build_model(hw0)
check("无EDID回退1080p档", m0.components["显示器"], round(1 * PM.STATIC["monitor_1440p"], 1))

hw0.gpu_resolution = "1920x1080"
check("无EDID回退1080p档(1080p)", PM.build_model(hw0).components["显示器"], 22.0)
hw0.gpu_resolution = "3840x2160"
check("无EDID回退4K档", PM.build_model(hw0).components["显示器"], 45.0)
hw0.gpu_resolution = "2560x1440"

# 有 monitors（单台 24.5"）→ 走面积模型
hw1 = _FakeHW()
hw1.monitors = [{"label": "通用即插即用监视器", "pnp": "SGT2450",
                 "w_cm": 54, "h_cm": 31, "inches": 24.5}]
m1 = PM.build_model(hw1)
check("单台24.5\"走面积模型", m1.components["显示器"], 30.3)
check("与旧档差异<1W(无跳变)", abs(m1.components["显示器"] - m0.components["显示器"]) < 1.0, True)

# 双屏：12" 小屏 + 27" 大屏，应分别计算而非 2×同档
hw2 = _FakeHW()
hw2.monitors = [{"label": "A", "pnp": "X", "w_cm": 26, "h_cm": 15, "inches": 11.9},
                {"label": "B", "pnp": "Y", "w_cm": 60, "h_cm": 34, "inches": 27.0}]
hw2.monitor_count = 2
m2 = PM.build_model(hw2)
_ws = [PM.monitor_watts(x["inches"], x["w_cm"], x["h_cm"], 1440) for x in hw2.monitors]
check("双屏分别计算", m2.components["显示器"], round(sum(_ws), 1))
check("双屏≠2×同档", m2.components["显示器"] != round(2 * PM.STATIC["monitor_1440p"], 1), True)

# 台数(count) 与 EDID 台数不一致时：测到尺寸的按面积，多出的按分辨率档补齐
# （headless 的「再接一台显示器」走的就是这条路径）
hw3 = _FakeHW()
hw3.monitors = [{"label": "A", "pnp": "X", "w_cm": 54, "h_cm": 31, "inches": 24.5}]
hw3.monitor_count = 2                      # 枚举到 2 台，仅 1 台有 EDID
m3 = PM.build_model(hw3)
_expected = round(PM.monitor_watts(24.5, 54, 31, 1440) + PM.STATIC["monitor_1440p"], 1)
check("台数>EDID台数时按档补齐", m3.components["显示器"], _expected)
check("补齐后台数确实增加", m3.components["显示器"] > m1.components["显示器"], True)

# 单台存在但无 EDID（远程桌面/虚拟机）→ 完全回退到旧档，结果必须与旧版一致
hw4 = _FakeHW()
hw4.monitors = [{"label": "A", "pnp": "X", "w_cm": 0, "h_cm": 0, "inches": None}]
hw4.monitor_count = 1
check("monitor存在但无EDID时回退", PM.build_model(hw4).components["显示器"], 30.0)


# ---------------------------------------------------------------- v18.36 传感器解析
print("--- v18.36 传感器(LHM Web Server / WMI 回退) ---")
# LHM /data.json 树解析：温度分类 + 坏通道过滤 + 风扇
_lhm = """{"Text":"Root","Children":[
 {"Text":"PC","Children":[
  {"Text":"B450M","HardwareId":"/motherboard","Children":[
   {"Text":"NCT6793D","HardwareId":"/lpc/nct6793d/0","Children":[
    {"Text":"Temperatures","Children":[
     {"Text":"Temperature #1","Type":"Temperature","Value":"28.5 °C"},
     {"Text":"Temperature #4","Type":"Temperature","Value":"110.0 °C"}]},
    {"Text":"Fans","Children":[
     {"Text":"Fan #1","Type":"Fan","Value":"1937 RPM"},
     {"Text":"Fan #2","Type":"Fan","Value":"0 RPM"}]}]}]},
  {"Text":"Ryzen 5600X","HardwareId":"/amdcpu/0","Children":[
   {"Text":"Temperatures","Children":[
    {"Text":"CCD1 (Tdie)","Type":"Temperature","Value":"76.8 °C"},
    {"Text":"Core (Tctl/Tdie)","Type":"Temperature","Value":"79.6 °C"},
    {"Text":"Bogus","Type":"Temperature","Value":"-40 °C"}]}]},
  {"Text":"RAM","HardwareId":"/ram/0","Children":[]}]}]}"""
_f, _t, _r = H._parse_lhm_json(_lhm)
check("LHM解析-ready", _r, True)
check("LHM解析-CPU优先Tctl", _t.get("cpu"), 79.6)          # Tctl 优先于 CCD1
check("LHM解析-主板温度", _t.get("motherboard"), 28.5)
check("LHM解析-坏通道110°被过滤", "motherboard" in _t and _t["motherboard"] <= 100.0, True)
check("LHM解析-无效-40°不计", _t.get("cpu"), 79.6)
check("LHM解析-风扇取非零", _f, [("Fan #1", 1937)])
_f, _t, _r = H._parse_lhm_json("not json")
check("LHM解析-垃圾输入", (_f, _t, _r), ([], {}, False))
# WMI 回退解析（OpenHardwareMonitor 老 JSON）
_f, _t = H._parse_sensors('[{"Name":"CPU Fan","SensorType":"Fan","Value":1200.4},'
                          '{"Name":"CPU Package","SensorType":"Temperature","Value":45.6},'
                          '{"Name":"GPU","SensorType":"Fan","Value":0}]')
check("WMI回退-风扇过滤0值", _f, [("CPU Fan", 1200)])
check("WMI回退-CPU温度", _t.get("cpu"), 45.6)
_r1 = H.fan_rpms_cached()
_r2 = H.fan_rpms_cached()
check("fans缓存-命中一致", _r1 == _r2, True)   # 第二次应走缓存（不重复请求）


print()
print("--- v18.37 LHM 传感器树 / 参考读数 ---")
# 用含 GPU/NVMe 的真实结构样例（含 SensorId，与 LHM Web Server 输出同构）
_tree = {"Text": "Sensor", "Children": [
  {"Text": "PC", "Children": [
    {"Text": "B450M-PLUS", "HardwareId": "/motherboard", "Children": [
      {"Text": "NCT6793D", "HardwareId": "/lpc/nct6793d/0", "Children": [
        {"Text": "Temperatures", "Children": [
          {"Text": "Temperature #1", "Type": "Temperature", "Value": "27.5 °C",
           "SensorId": "/lpc/nct6793d/0/temperature/1"},
          {"Text": "Temperature #4", "Type": "Temperature", "Value": "110.0 °C",
           "SensorId": "/lpc/nct6793d/0/temperature/4"}]},
        {"Text": "Fans", "Children": [
          {"Text": "Fan #1", "Type": "Fan", "Value": "1942 RPM",
           "SensorId": "/lpc/nct6793d/0/fan/0"},
          {"Text": "Fan #2", "Type": "Fan", "Value": "0 RPM",
           "SensorId": "/lpc/nct6793d/0/fan/1"}]}]}]},
    {"Text": "Ryzen 5600X", "HardwareId": "/amdcpu/0", "Children": [
      {"Text": "Temperatures", "Children": [
        {"Text": "Core (Tctl/Tdie)", "Type": "Temperature", "Value": "72.3 °C",
         "SensorId": "/amdcpu/0/temperature/0"},
        {"Text": "CCD1 (Tdie)", "Type": "Temperature", "Value": "61.5 °C",
         "SensorId": "/amdcpu/0/temperature/1"}]}]},
    {"Text": "GTX 1080", "HardwareId": "/gpu-nvidia/0", "Children": [
      {"Text": "GPU Hot Spot", "Type": "Temperature", "Value": "52.9 °C",
       "SensorId": "/gpu-nvidia/0/temperature/1"},
      {"Text": "GPU Core", "Type": "Temperature", "Value": "41.0 °C",
       "SensorId": "/gpu-nvidia/0/temperature/0"},
      {"Text": "GPU", "Type": "Fan", "Value": "1209 RPM",
       "SensorId": "/gpu-nvidia/0/fan/0"}]},
    {"Text": "SSD", "HardwareId": "/nvme/3", "Children": [
      {"Text": "Composite Temperature", "Type": "Temperature", "Value": "39.0 °C",
       "SensorId": "/nvme/3/temperature/0"},
      {"Text": "Warning Temperature", "Type": "Temperature", "Value": "67.0 °C",
       "SensorId": "/nvme/3/temperature/1"}]}]}]}
_gs = H._build_lhm_tree(_tree)
check("LHM树-硬件分组数", len(_gs), 5)          # 主板/SuperIO/CPU/GPU/NVMe
check("LHM树-父硬件名", _gs[1].get("parent"), "B450M-PLUS")
check("LHM树-子芯片名", _gs[1].get("name"), "NCT6793D")
check("LHM树-传感器数", len(_gs[1]["sensors"]), 4)          # 2 温度 + 2 风扇

_f, _t, _r = H._parse_lhm_json(json.dumps(_tree))   # 入参是 JSON 文本
check("LHM解析-GPU取Core", _t.get("gpu"), 41.0)             # Core 优先于 Hot Spot
check("LHM解析-同时给出CPU", _t.get("cpu"), 72.3)
check("LHM解析-主板", _t.get("motherboard"), 27.5)

# lhm_probe：供 UI tooltip 用的「LHM 参考读数」
H._LHM_SENS.update(groups=_gs, ts=0.0, ok=True)
check("probe-CPU参考", H.lhm_probe("cpu"),
      [("Ryzen 5600X · Core (Tctl/Tdie)", "72.3 °C"),
       ("Ryzen 5600X · CCD1 (Tdie)", "61.5 °C")])
check("probe-风扇只取转速>0",
      H.lhm_probe("fan"),
      [("NCT6793D · Fan #1", "1942 RPM"), ("GTX 1080 · GPU", "1209 RPM")])
check("probe-主板过滤坏通道110°",
      H.lhm_probe("motherboard"), [("NCT6793D · Temperature #1", "27.5 °C")])
check("probe-磁盘过滤告警阈值",
      H.lhm_probe("disk"), [("SSD · Composite Temperature", "39.0 °C")])
check("probe-无温度探头返回空", H.lhm_probe("memory"), [])
H._LHM_SENS.update(groups=[], ok=False)

# 回归：v18.36 的 ensure_lhm 用了 os.path.exists，但模块曾漏 import os
import builtins as _bi
check("回归-hardware已import os", hasattr(H, "os"), True)


print()
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
for f in FAIL:
    print("  FAIL " + f)
if FAIL:
    sys.exit(1)
print("HW_DETECT_OK")
