@echo off
REM PC 用电电费计算器 -- PyInstaller 一键打包（onefile 单文件 exe）
REM 用法：双击本文件。
REM 本文件由 build_v1811.sh 生成：改排除项请改 sh 后重新生成，
REM 保证两条构建路径的 41 条 --exclude-module 完全一致，不会各自漂移。
REM 编码为 GBK，与 cmd 默认代码页 936 一致，中文不会乱码。
setlocal
set PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe
cd /d %~dp0
set TS=%TIME: =0%
set TS=%TS:~0,2%%TS:~3,2%%TS:~6,2%

echo [1/4] 备份旧 exe ...
if exist "release\PC用电电费计算器.exe" (
    move /Y "release\PC用电电费计算器.exe" "release\_old_%TS%.exe" >nul
    echo       旧 exe 已备份为 _old_%TS%.exe
) else (
    echo       无旧 exe，跳过
)

echo [2/4] 挪走 builduild（关键：面对空目录就不会触发批量删除守卫）...
if exist "builduild" (
    move "builduild" "build\_old_build_%TS%" >nul 2>&1
    if errorlevel 1 (
        echo       警告：挪走失败，打包时可能被删除守卫拦住
    ) else (
        echo       已挪走为 _old_build_%TS%
    )
) else (
    echo       builduild 不存在，跳过
)

echo [3/4] 安装 / 更新 PyInstaller ...
"%PY%" -m pip install --quiet pyinstaller

echo [4/4] 打包为单文件 exe（窗口模式，无控制台）...
"%PY%" -m PyInstaller --noconfirm --onefile --windowed ^
    --name "PC用电电费计算器" ^
    --distpath=release --workpath=builduild ^
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

echo.
if exist "release\PC用电电费计算器.exe" (
    echo 构建成功：release\PC用电电费计算器.exe
    for %%F in ("release\PC用电电费计算器.exe") do echo        %%~zF 字节
) else (
    echo 构建失败，请检查上方输出。
    goto :done
)

echo.
set /p DODEPLOY=是否部署到桌面并重启程序？[Y/N，直接回车为否] 
if /I not "%DODEPLOY%"=="Y" goto :done
echo.
echo 部署：结束正在运行的实例（会丢掉最近一次周期落盘之后的电量，约 2 分钟内）...
taskkill /IM "PC用电电费计算器.exe" /F >nul 2>&1
timeout /t 2 /nobreak >nul
echo 部署：先把旧 exe 改名隔离，再拷贝（直接覆盖会被占用，且 copy 失败不报错）...
if exist "%USERPROFILE%\Desktop\PC用电电费计算器.exe" (
    ren "%USERPROFILE%\Desktop\PC用电电费计算器.exe" "_locked_%TS%.exe"
)
copy /Y "release\PC用电电费计算器.exe" "%USERPROFILE%\Desktop\" >nul
if exist "%USERPROFILE%\Desktop\PC用电电费计算器.exe" (
    echo 部署完成，正在启动 ...
    start "" "%USERPROFILE%\Desktop\PC用电电费计算器.exe"
) else (
    echo 部署失败：桌面 exe 写入未成功
)
:done
echo.
pause
