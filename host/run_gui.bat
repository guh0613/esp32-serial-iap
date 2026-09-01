@echo off
REM Double-clickable launcher for the Serial IAP GUI (Windows).
REM
REM Runs from the project root so that "host.gui" resolves as a package module,
REM and pins the interpreter to host\.venv rather than the ESP-IDF Python.
chcp 65001 >nul

cd /d "%~dp0.."

set "PYTHON=host\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo 找不到 %CD%\%PYTHON%
    echo.
    echo 请先创建上位机虚拟环境（在项目根目录下执行）：
    echo     python -m venv host\.venv
    echo     host\.venv\Scripts\python -m pip install -r host\requirements.txt
    echo.
    pause
    exit /b 1
)

"%PYTHON%" -B -m host.gui
if errorlevel 1 pause
