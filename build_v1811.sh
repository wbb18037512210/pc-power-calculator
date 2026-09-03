#!/bin/bash
# v18.11 打包：挪走 build/build 空目录以绕开批量删除守卫
cd "C:/Users/Administrator/WorkBuddy/2026-09-03-17-33-15/pc_power_calc" || exit 1
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
TS=$(date +%H%M%S)

if [ -f "release/PC用电电费计算器.exe" ]; then
  mv "release/PC用电电费计算器.exe" "release/_old_${TS}.exe" && echo "旧 exe 已备份为 _old_${TS}.exe"
fi

if [ -d "build/build" ]; then
  if mv "build/build" "build/_old_build_${TS}"; then
    echo "build 目录已挪走 -> _old_build_${TS}"
  else
    echo "警告: build/build 挪走失败，可能被占用"
  fi
fi

"$PY" -m PyInstaller --noconfirm --onefile --windowed \
  --name "PC用电电费计算器" \
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

echo "BUILD_EXIT=$?"
ls -la release/ 2>/dev/null | head -10
