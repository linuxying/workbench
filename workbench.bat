@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 如需自定义端口，去掉下一行注释并修改数字（如被占用时）：
REM set PORT=5099

REM 启动免安装工作台（无控制台窗口，浏览器自动打开）
start "" workbench.exe
