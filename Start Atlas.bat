@echo off
rem Double-click this file to start Atlas on Windows. Keep the window open while you use it.
title Atlas - keep this window open
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
if errorlevel 1 pause
