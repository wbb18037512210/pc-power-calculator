# -*- coding: utf-8 -*-
"""v18.37 打磨：
1) LHM 传感器树保留父硬件名（B450M-PLUS › Nuvoton NCT6793D），与 LHM 树形一致。
2) 温度列 tooltip 区分「LHM 未运行」与「该部件本身没有温度探头」（如内存）。
"""
import io, os

ROOT = os.path.dirname(os.path.abspath(__file__))


def rw(name):
    p = os.path.join(ROOT, name)
    return p, io.open(p, "r", encoding="utf-8").read()


def save(p, s):
    io.open(p, "w", encoding="utf-8").write(s)


# ------------------------------ hardware.py ------------------------------
p, s = rw("hardware.py")

old = '''    def visit(node, cur):
        hwid = node.get("HardwareId")
        sid = node.get("SensorId")
        if hwid:
            cur = {"name": str(node.get("Text") or ""), "hwid": str(hwid),
                   "sensors": []}
            groups.append(cur)
        elif sid and cur is not None:'''
new = '''    def visit(node, cur, parent=""):
        hwid = node.get("HardwareId")
        sid = node.get("SensorId")
        if hwid:
            cur = {"name": str(node.get("Text") or ""), "hwid": str(hwid),
                   "parent": parent, "sensors": []}
            groups.append(cur)
            parent = str(node.get("Text") or "")
        elif sid and cur is not None:'''
if new not in s:
    assert old in s, "hardware: visit 未找到"
    s = s.replace(old, new, 1)

old = '''        for c in node.get("Children") or []:
            visit(c, cur)

    visit(root, None)
    return groups'''
new = '''        for c in node.get("Children") or []:
            visit(c, cur, parent)

    visit(root, None)
    return groups'''
if new not in s:
    assert old in s, "hardware: visit 递归未找到"
    s = s.replace(old, new, 1)

save(p, s)
assert '"parent": parent' in s
print("hardware.py OK")

# -------------------------------- main.py --------------------------------
p, s = rw("main.py")

# 1) tooltip 文案分情况
old = '''        if not rows:
            return ("LibreHardwareMonitor 无该项读数\\n"
                    "（Windows 免驱读不到 SuperIO/EC，需运行 %s）" % H._LHM_EXE)
        return ("LibreHardwareMonitor 参考读数\\n"
                + "\\n".join("• %s：%s" % (a, b) for a, b in rows[:14]))'''
new = '''        if not rows:
            try:
                _ok = H.lhm_sensors().get("ok")
            except Exception:
                _ok = False
            if not _ok:
                return ("未连接到 LibreHardwareMonitor\\n"
                        "温度/转速需经其 Web Server（127.0.0.1:8085）读取\\n"
                        "（Windows 免驱读不到 SuperIO/EC 芯片）")
            return ("LibreHardwareMonitor 未报告「%s」的温度/转速\\n"
                    "该部件本身通常不带温度探头（属正常现象）" % s)
        return ("LibreHardwareMonitor 参考读数\\n"
                + "\\n".join("• %s：%s" % (a, b) for a, b in rows[:14]))'''
if new not in s:
    assert old in s, "main: tooltip 文案未找到"
    s = s.replace(old, new, 1)

# 2) 详情窗口标题带父硬件（与 LHM 树形一致）
old = '''                h = QTableWidgetItem(str(g.get("name") or ""))'''
new = '''                _p = str(g.get("parent") or "")
                _title = (f"{_p} › {g.get('name')}" if _p and _p != g.get("name")
                          else str(g.get("name") or ""))
                h = QTableWidgetItem(_title)'''
if new not in s:
    assert old in s, "main: 分组标题未找到"
    s = s.replace(old, new, 1)

save(p, s)
assert "未连接到 LibreHardwareMonitor" in s and "_p} › {" in s
print("main.py OK")
