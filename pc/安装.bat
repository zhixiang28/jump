@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 安装需要的 Python 套件...
python -m pip install -r requirements.txt
echo.
echo 完成。之后双击「开始.bat」
pause
