@echo off
rem Set up and use Atlas from a command prompt. Type "atlas" for the list of commands.
setlocal
set "ROOT=%~dp0"
if /i "%~1"=="start" (
  call "%ROOT%Start Atlas.bat"
  exit /b
)
if not exist "%ROOT%backend\.venv\Scripts\python.exe" (
  echo Atlas isn't installed yet. Run: atlas start
  exit /b 1
)
"%ROOT%backend\.venv\Scripts\python.exe" "%ROOT%scripts\atlas.py" %*
