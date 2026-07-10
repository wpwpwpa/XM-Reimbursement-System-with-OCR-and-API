@echo off
cd /d "%~dp0"
"C:\Users\10516\.workbuddy\binaries\python\envs\default\Scripts\python.exe" app\main.py
if errorlevel 1 pause
