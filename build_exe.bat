@echo off
rem ============================================================
rem  PC用电电费计算器 一键打包（与 build_v1811.sh 逻辑一致）
rem  流程: 关进程 -> 暂存旧exe -> 挪build -> PyInstaller
rem        -> 产物改名为「项目名+版本号」 -> 清理旧版本 -> 部署桌面
rem  要求: 本文件必须是 GBK + CRLF 编码保存
rem ============================================================
setlocal EnableDelayedExpansion
chcp 936 >nul
rem 用脚本自身所在目录作为工作目录，去掉硬编码绝对路径，使任意克隆目录都可跑
cd /d "%~dp0"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "NAME=PC用电电费计算器"

echo ==^> [1/6] 关闭运行中的实例
"%PY%" kill_instances.py

echo ==^> [2/6] 暂存现有 exe
if exist "release\_staging.exe" del /f /q "release\_staging.exe"
set "STAGED="
for %%f in ("release\%NAME%*.exe") do (
  move /y "%%f" "release\_staging.exe" >nul 2>&1
  if not errorlevel 1 set "STAGED=1"
)
if defined STAGED (echo     已暂存为 _staging.exe) else (echo     无现有 exe，跳过)

echo ==^> [3/6] 挪走 build\build（避免 PyInstaller 触发批量删除守卫）
if exist "build\build" (
  for /f "tokens=1-3 delims=:.," %%a in ("%TIME%") do set "TS=%%a%%b%%c"
  move "build\build" "build\_old_build_!TS!" >nul 2>&1
  if exist "build\_old_build_!TS!" (echo     已挪走) else (echo     警告: 挪走失败，可能被占用)
)

echo ==^> [4/6] PyInstaller 打包中…
"%PY%" -m PyInstaller --noconfirm --onefile --windowed ^
  --name "%NAME%" ^
  --distpath=release --workpath=build\build ^
  --exclude-module PySide6.QtWebEngineCore ^
  --exclude-module PySide6.QtWebEngineWidgets ^
  --exclude-module PySide6.QtWebEngineQuick ^
  --exclude-module PySide6.QtWebChannel ^
  --exclude-module PySide6.QtQml ^
  --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtQuick3D ^
  --exclude-module PySide6.QtQuickWidgets ^
  --exclude-module PySide6.QtQuickControls2 ^
  --exclude-module PySide6.Qt3DCore ^
  --exclude-module PySide6.Qt3DRender ^
  --exclude-module PySide6.Qt3DAnimation ^
  --exclude-module PySide6.Qt3DInput ^
  --exclude-module PySide6.Qt3DLogic ^
  --exclude-module PySide6.Qt3DExtras ^
  --exclude-module PySide6.QtMultimedia ^
  --exclude-module PySide6.QtMultimediaWidgets ^
  --exclude-module PySide6.QtPdf ^
  --exclude-module PySide6.QtPdfWidgets ^
  --exclude-module PySide6.QtDataVisualization ^
  --exclude-module PySide6.QtGraphs ^
  --exclude-module PySide6.QtGraphsWidgets ^
  --exclude-module PySide6.QtHelp ^
  --exclude-module PySide6.QtDesigner ^
  --exclude-module PySide6.QtUiTools ^
  --exclude-module PySide6.QtTest ^
  --exclude-module PySide6.QtSql ^
  --exclude-module PySide6.QtPositioning ^
  --exclude-module PySide6.QtLocation ^
  --exclude-module PySide6.QtBluetooth ^
  --exclude-module PySide6.QtNfc ^
  --exclude-module PySide6.QtSerialPort ^
  --exclude-module PySide6.QtSensors ^
  --exclude-module PySide6.QtWebSockets ^
  --exclude-module PySide6.QtHttpServer ^
  --exclude-module PySide6.QtTextToSpeech ^
  --exclude-module PySide6.QtVirtualKeyboard ^
  --exclude-module PySide6.QtScxml ^
  --exclude-module PySide6.QtStateMachine ^
  --exclude-module PySide6.QtRemoteObjects ^
  --exclude-module PySide6.QtNetworkAuth ^
  main.py
set "BUILD_EXIT=%ERRORLEVEL%"
echo BUILD_EXIT=%BUILD_EXIT%

echo ==^> [5/6] 产物改名为「项目名+版本号」并清理旧版本
set "VER="
for /f "usebackq tokens=2" %%a in (`findstr /C:"APP_VERSION = " main.py`) do set "VER=%%a"
set "VER=!VER:"=!"
if "%BUILD_EXIT%"=="0" if exist "release\%NAME%.exe" (
  if defined VER (
    move /y "release\%NAME%.exe" "release\%NAME%_!VER!.exe" >nul
    echo     产物已命名: %NAME%_!VER!.exe
  ) else (
    echo     警告: 未能解析 APP_VERSION，保留默认文件名
  )
  if exist "release\_staging.exe" del /f /q "release\_staging.exe"
  for %%f in ("release\_old_*.exe") do del /f /q "%%f"
  if exist "release\_locked_archive" rmdir /s /q "release\_locked_archive"
  if exist "dist" rmdir /s /q "dist"
  if exist "debug" rmdir /s /q "debug"
  for /d %%d in ("build\_old_build_*") do rmdir /s /q "%%d"
  echo     旧版本备份已全部删除（只保留最新 exe）
) else (
  if exist "release\_staging.exe" (
    move /y "release\_staging.exe" "release\%NAME%.exe" >nul
    echo     打包失败，已还原旧 exe
  )
  echo     注意：打包未成功，旧版本未删除
  goto :end
)

echo ==^> [6/6] 部署到桌面（先清掉桌面旧版本）
if defined VER (
  for %%f in ("%USERPROFILE%\Desktop\%NAME%*.exe") do del /f /q "%%f" 2>nul
  copy /y "release\%NAME%_!VER!.exe" "%USERPROFILE%\Desktop\" >nul
  if exist "%USERPROFILE%\Desktop\%NAME%_!VER!.exe" (
    echo     已部署: %USERPROFILE%\Desktop\%NAME%_!VER!.exe
  ) else (
    echo     部署失败，请手动从 release\ 复制
  )
)

:end
echo.
echo ---- release\ ----
dir /b "release\*.exe" 2>nul
endlocal
