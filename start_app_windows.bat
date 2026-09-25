@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3" && set "PYW=pyw -3"
if not defined PY where python >nul 2>nul && set "PY=python" && set "PYW=pythonw"
if not defined PY (
  echo.
  echo Python が見つかりません。
  echo https://www.python.org/downloads/ から無料で入れてください。
  echo 入れるときは「Add python.exe to PATH」にチェックを付けてください。
  echo.
  pause
  exit /b
)
%PY% -c "import PIL, pillow_heif" >nul 2>nul
if errorlevel 1 (
  echo 初回だけ、画像を表示する部品を入れています…
  %PY% -m pip install --user --quiet pillow pillow-heif
)
start "" %PYW% app.py
