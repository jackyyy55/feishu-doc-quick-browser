@echo off
chcp 65001 >nul
REM 打包成单文件 exe：dist\飞书文档快速浏览器 v1.0.exe
REM 依赖：pip install pyinstaller pywebview pillow pythonnet

cd /d "%~dp0"

if not exist "data\app.ico" (
  echo [!] 缺少 data\app.ico，先运行一次 app.py 让它自动生成图标
  pause
  exit /b 1
)

python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name "飞书文档快速浏览器 v1.0" ^
  --icon "data\app.ico" ^
  --add-data "web;web" ^
  --collect-submodules webview ^
  --hidden-import clr ^
  --hidden-import pythonnet ^
  app.py

if errorlevel 1 (
  echo [!] 打包失败
  pause
  exit /b 1
)

copy /y "dist\飞书文档快速浏览器 v1.0.exe" "飞书文档快速浏览器 v1.0.exe" >nul
echo.
echo [OK] 已生成：dist\飞书文档快速浏览器 v1.0.exe
echo      config.json / data 会在 exe 同级目录自动创建
pause
