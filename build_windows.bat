@echo off
REM Build Route Planner for Windows
REM Run this on a Windows PC: double-click or run from Command Prompt
REM Output: dist\Route Planner\Route Planner.exe  (zip and distribute)

cd /d "%~dp0"

echo =^> Installing / updating dependencies...
pip install -r requirements.txt

echo =^> Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo =^> Building executable...
pyinstaller route_planner.spec

echo =^> Packaging as zip...
powershell -Command "Compress-Archive -Path 'dist\Route Planner' -DestinationPath 'dist\Route Planner Windows.zip'"

echo.
echo Done!  Distributable: dist\Route Planner Windows.zip
echo Unzip ^> run 'Route Planner.exe' to launch.
pause
