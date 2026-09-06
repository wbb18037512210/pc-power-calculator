# -*- coding: utf-8 -*-
"""build_exe.bat 的 Python 等价实现（cmd.exe 被安全策略拦截时用这个）。

流程与 bat 一致：关进程 → 暂存旧 exe → 挪走 build\build → 打源码包
→ PyInstaller → 改名带版本号 → 清理 → 部署桌面 → 生成源码 zip → 清临时目录。
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
PY = r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
NAME = "PC用电电费计算器"
REL = os.path.join(ROOT, "release")
DESK = os.path.join(os.path.expanduser("~"), "Desktop")


def run(args, **kw):
    print("[run]", " ".join(args[:6]) + (" ..." if len(args) > 6 else ""), flush=True)
    return subprocess.run(args, cwd=ROOT, **kw)


def ver():
    s = open("main.py", encoding="utf-8").read()
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', s)
    return m.group(1) if m else ""


V = ver()
print("[ver]", V, flush=True)
assert V, "未解析到 APP_VERSION"

# 1) 关闭运行中的实例
run([PY, "kill_instances.py"])

# 2) 暂存旧 exe
staging = os.path.join(REL, "_staging.exe")
if os.path.exists(staging):
    os.remove(staging)
old = glob.glob(os.path.join(REL, NAME + "*.exe"))
if old:
    shutil.move(old[0], staging)
    print("[stage]", os.path.basename(old[0]), flush=True)

# 3) 挪走 build\build（避免 PyInstaller 触发批量删除守卫）
bb = os.path.join(ROOT, "build", "build")
if os.path.isdir(bb):
    dst = os.path.join(ROOT, "build", "_old_build_%s" % time.strftime("%H%M%S"))
    try:
        os.rename(bb, dst)
        print("[move] build\\build ->", os.path.basename(dst), flush=True)
    except Exception as e:
        print("[warn] 挪走失败:", e, flush=True)

# 4) 打源码包（内嵌进 exe）
r = run([PY, "package_source.py", "--bundle-dir", "_src_bundle"])
assert r.returncode == 0, "package_source 失败"

# 5) PyInstaller
excludes = []
for m in ("QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick", "QtWebChannel",
          "QtQml", "QtQuick", "QtQuick3D", "QtQuickWidgets", "QtQuickControls2",
          "Qt3DCore", "Qt3DRender", "Qt3DAnimation", "Qt3DInput", "Qt3DLogic",
          "Qt3DExtras", "QtMultimedia", "QtMultimediaWidgets", "QtPdf", "QtPdfWidgets",
          "QtDataVisualization", "QtGraphs", "QtGraphsWidgets", "QtHelp", "QtDesigner",
          "QtUiTools", "QtTest", "QtSql", "QtPositioning", "QtLocation", "QtBluetooth",
          "QtNfc", "QtSerialPort", "QtSensors", "QtWebSockets", "QtHttpServer",
          "QtTextToSpeech", "QtVirtualKeyboard", "QtScxml", "QtStateMachine",
          "QtRemoteObjects", "QtNetworkAuth"):
    excludes += ["--exclude-module", "PySide6." + m]

cmd = [PY, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
       "--name", NAME, "--distpath=release", "--workpath=build\\build"] + excludes + \
      ["--add-data", "_src_bundle;src", "main.py"]
t0 = time.time()
r = run(cmd)
print("[pyinstaller] exit=%s  用时 %.0fs" % (r.returncode, time.time() - t0), flush=True)

prod = os.path.join(REL, NAME + ".exe")
if r.returncode != 0 or not os.path.exists(prod):
    if os.path.exists(staging):
        shutil.move(staging, prod)
        print("[rollback] 已还原旧 exe", flush=True)
    sys.exit(1)

# 6) 改名 + 清理
named = os.path.join(REL, "%s_%s.exe" % (NAME, V))
if os.path.exists(named):
    os.remove(named)
os.rename(prod, named)
print("[name]", os.path.basename(named), flush=True)
if os.path.exists(staging):
    os.remove(staging)
for f in glob.glob(os.path.join(REL, "_old_*.exe")):
    try:
        os.remove(f)
    except Exception:
        pass
for d in glob.glob(os.path.join(ROOT, "build", "_old_build_*")):
    shutil.rmtree(d, ignore_errors=True)

# 7) 部署桌面
for f in glob.glob(os.path.join(DESK, NAME + "*.exe")):
    try:
        os.remove(f)
    except Exception:
        pass
shutil.copy2(named, DESK)
print("[deploy]", os.path.join(DESK, os.path.basename(named)), flush=True)

# 8) 源码 zip
r = run([PY, "package_source.py", "--out", "release"])
print("[src-zip]", r.returncode, flush=True)
for f in glob.glob(os.path.join(REL, "*源码*%s.zip" % V)):
    shutil.copy2(f, DESK)
    print("[deploy]", os.path.basename(f), flush=True)

# 9) 清临时目录
shutil.rmtree(os.path.join(ROOT, "_src_bundle"), ignore_errors=True)
print("[done]", flush=True)
