@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 先自动更新再打开程序（全部写在同一行：更新会换掉这个档案，cmd 不会读到一半的新档）
python start.py || pause & exit /b
