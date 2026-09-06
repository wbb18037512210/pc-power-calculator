# -*- coding: utf-8 -*-
"""v18.37 单测：LHM 传感器树 / 参考读数（lhm_probe）/ GPU 语义温度 / os 回归。"""
import io, os

ROOT = os.path.dirname(os.path.abspath(__file__))
p = os.path.join(ROOT, "tests", "test_hw_detect.py")
s = io.open(p, "r", encoding="utf-8").read()

block = '''
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

_f, _t, _r = H._parse_lhm_json(_tree)
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

'''

anchor = '\nprint()\nprint(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")'
if "v18.37 LHM 传感器树" not in s:
    assert anchor in s, "锚点未找到"
    s = s.replace(anchor, block + anchor, 1)
    io.open(p, "w", encoding="utf-8").write(s)

assert "LHM树-硬件分组数" in io.open(p, "r", encoding="utf-8").read()
print("tests OK")
