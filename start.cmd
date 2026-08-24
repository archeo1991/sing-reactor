@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "START_EXIT_CODE=%ERRORLEVEL%"

if not "%START_EXIT_CODE%"=="0" (
  echo.
  echo Sing Reactor 启动失败，退出代码：%START_EXIT_CODE%
  echo 请查看上方错误信息以及项目根目录日志。
)

exit /b %START_EXIT_CODE%
