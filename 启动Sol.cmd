@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0build\Sol\Sol.exe" (
  echo 尚未构建 Sol，请先运行安装Sol.cmd
  pause
  exit /b 1
)
start "" "%~dp0build\Sol\Sol.exe"
