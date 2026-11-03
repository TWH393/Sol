@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 请先从托盘完全退出 Sol。本更新不会自动关闭程序。
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update-installed.ps1"
if errorlevel 1 (
  echo 未完成更新，请按上方具体原因处理后重新运行。
  pause
  exit /b 1
)
echo 更新完成，请从桌面 Sol 快捷方式打开，由你自行验收。
pause
