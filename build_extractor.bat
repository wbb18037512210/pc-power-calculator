@echo off
rem ============================================================
rem  SourceExtractor 一键打包（从主程序 exe 提取内嵌源码的小工具）
rem  产物：release/SourceExtractor.exe -> 部署到桌面
rem  说明：extract_source.py 仅依赖标准库（自带极简 CArchive 读取器），
rem        不引用 PySide6 / PyInstaller，因此体积极小、与主程序完全独立。
rem ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "NAME=SourceExtractor"

echo ==^> Building %NAME% (standalone source extractor)...
"%PY%" -m PyInstaller --noconfirm --onefile --windowed ^
  --name "%NAME%" ^
  --distpath=release --workpath=build/extractor_build ^
  --exclude-module PySide6 --exclude-module PyQt6 --exclude-module PyQt5 ^
  --exclude-module tkinter --exclude-module numpy --exclude-module PIL ^
  extract_source.py
if "%ERRORLEVEL%"=="0" (
  if exist "release\%NAME%.exe" (
    copy /y "release\%NAME%.exe" "%USERPROFILE%\Desktop\" >nul
    echo deployed: %USERPROFILE%\Desktop\%NAME%.exe
  )
) else (
  echo BUILD FAILED
)
endlocal
