@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 fukugen.py %*
  goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
  python fukugen.py %*
  goto :eof
)
echo.
echo Python が見つかりません。
echo https://www.python.org/downloads/ から無料で入れてください。
echo 入れるときは「Add python.exe to PATH」にチェックを付けてください。
echo.
pause
