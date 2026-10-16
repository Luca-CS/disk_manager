@echo off
setlocal
rem Lance Disk Manager (sans fenetre de console). Options : --dry-run (simulation)
set "PYW="
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*") do if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
if not defined PYW for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
if not defined PYW for %%P in (pythonw.exe) do set "PYW=%%~$PATH:P"
if not defined PYW (
  echo Python 3.10 ou plus recent est introuvable. Installez-le depuis python.org puis relancez.
  pause
  exit /b 1
)
start "" "%PYW%" "%~dp0run.py" %*
