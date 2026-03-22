@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动通用叙事游戏引擎 GUI版本...
python gui.py
pause
