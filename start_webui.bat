@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 goto :use_py
where python >nul 2>nul
if not errorlevel 1 goto :use_python

echo 未找到 Python。请先安装 Python 3.10 或更新版本。
pause
exit /b 1

:use_py
py -3 "%~dp0webui\server.py" %*
goto :finished

:use_python
python "%~dp0webui\server.py" %*

:finished
pause
