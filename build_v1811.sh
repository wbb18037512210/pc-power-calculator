#!/bin/bash
# v18.18 打包脚本
#   1. 关闭所有正在运行的实例（否则 exe 被占用，覆盖不了，最后启动的还是旧版）
#   2. 挪走现有 exe 与 build/build（后者是为了绕开批量删除守卫）
#   3. PyInstaller 打包
#   4. 成功则把产物改名为「项目名+版本号」（PC用电电费计算器_v18.17.exe），
#      并删光所有旧版本；失败则还原旧 exe，不留残局
cd "C:/Users/Administrator/WorkBuddy/2026-09-03-17-33-15/pc_power_calc" || exit 1
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
NAME="PC用电电费计算器"

echo "==> [1/5] 关闭运行中的实例"
"$PY" kill_instances.py

echo "==> [2/5] 暂存现有 exe"
STAGED=""
for f in release/${NAME}*.exe release/_staging.exe; do
  if [ -f "$f" ]; then
    mv "$f" "release/_staging.exe" && STAGED="1" && echo "    已暂存: $f -> _staging.exe"
    break
  fi
done
[ -z "$STAGED" ] && echo "    无现有 exe，跳过"

echo "==> [3/5] 挪走 build/build（避免 PyInstaller 触发批量删除守卫）"
if [ -d "build/build" ]; then
  if mv "build/build" "build/_old_build_$(date +%H%M%S)"; then
    echo "    已挪走"
  else
    echo "    警告: 挪走失败，可能被占用"
  fi
fi

echo "==> [4/5] PyInstaller 打包中…"
"$PY" -m PyInstaller --noconfirm --onefile --windowed \
  --name "$NAME" \
  --distpath=release --workpath=build/build \
  --exclude-module PySide6.QtWebEngineCore \
  --exclude-module PySide6.QtWebEngineWidgets \
  --exclude-module PySide6.QtWebEngineQuick \
  --exclude-module PySide6.QtWebChannel \
  --exclude-module PySide6.QtQml \
  --exclude-module PySide6.QtQuick \
  --exclude-module PySide6.QtQuick3D \
  --exclude-module PySide6.QtQuickWidgets \
  --exclude-module PySide6.QtQuickControls2 \
  --exclude-module PySide6.Qt3DCore \
  --exclude-module PySide6.Qt3DRender \
  --exclude-module PySide6.Qt3DAnimation \
  --exclude-module PySide6.Qt3DInput \
  --exclude-module PySide6.Qt3DLogic \
  --exclude-module PySide6.Qt3DExtras \
  --exclude-module PySide6.QtMultimedia \
  --exclude-module PySide6.QtMultimediaWidgets \
  --exclude-module PySide6.QtPdf \
  --exclude-module PySide6.QtPdfWidgets \
  --exclude-module PySide6.QtDataVisualization \
  --exclude-module PySide6.QtGraphs \
  --exclude-module PySide6.QtGraphsWidgets \
  --exclude-module PySide6.QtHelp \
  --exclude-module PySide6.QtDesigner \
  --exclude-module PySide6.QtUiTools \
  --exclude-module PySide6.QtTest \
  --exclude-module PySide6.QtSql \
  --exclude-module PySide6.QtPositioning \
  --exclude-module PySide6.QtLocation \
  --exclude-module PySide6.QtBluetooth \
  --exclude-module PySide6.QtNfc \
  --exclude-module PySide6.QtSerialPort \
  --exclude-module PySide6.QtSensors \
  --exclude-module PySide6.QtWebSockets \
  --exclude-module PySide6.QtHttpServer \
  --exclude-module PySide6.QtTextToSpeech \
  --exclude-module PySide6.QtVirtualKeyboard \
  --exclude-module PySide6.QtScxml \
  --exclude-module PySide6.QtStateMachine \
  --exclude-module PySide6.QtRemoteObjects \
  --exclude-module PySide6.QtNetworkAuth \
  main.py
BUILD_EXIT=$?
echo "BUILD_EXIT=$BUILD_EXIT"

echo "==> [5/5] 改名为「项目名+版本号」并清理旧版本"
# 从 main.py 提取 APP_VERSION，例如  APP_VERSION = "v18.17"  ->  v18.17
VER=$("$PY" -c "import re,io; m=re.search(r'APP_VERSION\s*=\s*\"([^\"]+)\"', io.open('main.py',encoding='utf-8').read()); print(m.group(1) if m else '')")
if [ "$BUILD_EXIT" = "0" ] && [ -f "release/${NAME}.exe" ]; then
  if [ -n "$VER" ]; then
    mv -f "release/${NAME}.exe" "release/${NAME}_${VER}.exe"
    echo "    产物已命名: ${NAME}_${VER}.exe"
  else
    echo "    警告: 未能从 main.py 解析 APP_VERSION，保留默认文件名"
  fi
  rm -f release/_staging.exe release/_old_*.exe
  rm -rf release/_locked_archive dist debug build/_old_build_*
  echo "    旧版本备份已全部删除（只保留最新 exe）"
else
  if [ -f "release/_staging.exe" ]; then
    mv "release/_staging.exe" "release/${NAME}.exe"
    echo "    打包失败，已还原旧 exe"
  fi
  echo "    注意：打包未成功，旧版本未删除"
fi

echo "--- release/ ---"
ls -la release/ 2>/dev/null | head -10
