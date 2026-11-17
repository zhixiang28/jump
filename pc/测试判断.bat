@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 只测试截图和判断，不会按（结果图在 debug\latest.png）
python jump_bot.py --no-ble
pause
