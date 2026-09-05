# -*- coding: utf-8 -*-
"""v18.28 显示器真实电源事件：GUID 解析 / 结构体布局的纯逻辑回归测试。

不依赖真实窗口句柄或 Windows 电源事件，只验证：
  1. 字符串 GUID <-> _GUID 互转（大小写 / 花括号容错）；
  2. _POWERBROADCAST_SETTING 结构体字段偏移正确（nativeEvent 按此解析 lParam，
     字段错位会导致永远读不到显示器开关真值，复现「关屏不识别」）；
  3. WM / 电源事件常量值正确。
只要 ctypes 可用即可在任意平台跑（非 win32 也不触发注册，但结构定义常驻）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

MON = main._MONITOR_POWER_ON_GUID  # 标准大写带花括号


def test_guid_str_roundtrip():
    d1, d2, d3, b = main._parse_guid_str(MON)
    # 回填 _GUID 再转回字符串应与原串（大写标准格式）完全一致
    g = main._GUID()
    g.Data1, g.Data2, g.Data3 = d1, d2, d3
    for i in range(8):
        g.Data4[i] = b[i]
    assert main._guid_to_str(g) == MON, main._guid_to_str(g)
    # 小写 / 无花括号也应能解析成同一组字段
    low = MON.lower().strip("{}")
    d1b, d2b, d3b, bb = main._parse_guid_str(low)
    assert (d1b, d2b, d3b, list(bb)) == (d1, d2, d3, list(b))


def test_guid_struct_layout():
    import ctypes
    # _GUID 必须是 16 字节（4+2+2+8），否则与 Windows 二进制布局不符
    assert ctypes.sizeof(main._GUID) == 16


def test_powerbroadcast_setting_fields():
    import ctypes
    # 结构体总大小：GUID(16) + DataLength(4) + Data(4) = 24，且 Data 在末尾
    assert ctypes.sizeof(main._POWERBROADCAST_SETTING) == 24
    g = main._GUID()
    d1, d2, d3, b = main._parse_guid_str(MON)
    g.Data1, g.Data2, g.Data3 = d1, d2, d3
    for i in range(8):
        g.Data4[i] = b[i]
    pbs = main._POWERBROADCAST_SETTING()
    pbs.PowerSetting = g
    pbs.DataLength = 4
    pbs.Data = 0  # 0 = 显示器已物理关闭
    # GUID 字段能被精确还原并被 nativeEvent 的判定逻辑识别
    assert main._guid_to_str(pbs.PowerSetting) == MON
    # Data 字段为最末成员，偏移应在结构体尾（24 - 4 = 20）
    off = main._POWERBROADCAST_SETTING.Data.offset
    assert off == 20, off
    assert pbs.Data == 0
    pbs.Data = 1  # 1 = 开屏
    assert pbs.Data == 1


def test_event_constants():
    assert main._WM_POWERBROADCAST == 0x0218
    assert main._PBT_POWERSETTINGCHANGE == 0x8013
    assert main._DEVICE_NOTIFY_WINDOW_HANDLE == 0x00000000
    assert main._MONITOR_POWER_ON_GUID == "{0273105A-6A1B-4244-AD7A-3A0B30C60E5D}"


if __name__ == "__main__":
    test_guid_str_roundtrip()
    test_guid_struct_layout()
    test_powerbroadcast_setting_fields()
    test_event_constants()
    print("monitor_hook parsing OK")
