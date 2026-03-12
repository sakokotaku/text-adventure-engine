@echo off
chcp 65001 >nul
:: 优先用 Windows Terminal（支持行高），回退到普通 CMD
where wt >nul 2>&1
if %errorlevel%==0 (
    wt --title "通用叙事游戏引擎" cmd /k "chcp 65001 >nul && python \"%~dp0main.py\" && pause"
) else (
    python "%~dp0main.py"
    pause
)
