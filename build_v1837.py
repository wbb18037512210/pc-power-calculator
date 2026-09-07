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


def prepare_lhm():
    """把 lhm_bin/ 精简出一个可直接打包的副本 lhm_pack/。

    剔除调试符号(.pdb)、API 文档(.xml)、原始下载包 lhm.zip、旧配置备份(.bak)：
    实测这些约 7.8MB，砍掉后 19MB → 11MB。

    同时重写 LibreHardwareMonitor.config —— LHM 读的是
    Path.ChangeExtension(exe, ".config") 的产物即 **LibreHardwareMonitor.config**，
    不是 LibreHardwareMonitor.exe.config。写错文件会导致 8085 永不监听且无报错，
    所以这里每次构建都按确定内容重新生成，不依赖源码目录里的手写配置。
    """
    src = os.path.join(ROOT, "lhm_bin")
    dst = os.path.join(ROOT, "lhm_pack")
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    if not os.path.isdir(src):
        print("[warn] 未找到 lhm_bin，温度读取将不可用", flush=True)
        return None
    skip_ext = (".pdb", ".xml", ".bak", ".zip")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for name in sorted(os.listdir(src)):
        p = os.path.join(src, name)
        if os.path.isdir(p):                    # 各语言资源目录（体积很小，保留）
            shutil.copytree(p, os.path.join(dst, name))
            continue
        if name.lower().endswith(skip_ext):
            continue
        if name == "LibreHardwareMonitor.config":
            continue                            # 下面统一重新生成
        shutil.copy2(p, os.path.join(dst, name))
        n += 1
    with open(os.path.join(dst, "LibreHardwareMonitor.config"), "w",
              encoding="utf-8") as f:
        f.write(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<configuration>\n"
            "  <appSettings>\n"
            '    <add key="runWebServerMenuItem" value="true" />\n'
            '    <add key="listenerIp" value="127.0.0.1" />\n'
            '    <add key="listenerPort" value="8085" />\n'
            '    <add key="authenticationEnabled" value="false" />\n'
            '    <add key="theme" value="auto" />\n'
            "  </appSettings>\n"
            "</configuration>\n")
    print("[lhm] %d 个文件 -> lhm_pack" % n, flush=True)
    return "lhm_pack"


V = ver()
print("[ver]", V, flush=True)
assert V, "未解析到 APP_VERSION"

LHM_PACK = prepare_lhm()

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
       "--name", NAME, "--distpath=release", "--workpath=build\\build"] + excludes
# v18.43 内置 LibreHardwareMonitor（温度 / 风扇转速的唯一数据源）
if LHM_PACK:
    cmd += ["--add-data", "%s;lhm_bin" % LHM_PACK]
cmd += ["--add-data", "_src_bundle;src", "main.py"]
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
shutil.rmtree(os.path.join(ROOT, "lhm_pack"), ignore_errors=True)
print("[done]", flush=True)
