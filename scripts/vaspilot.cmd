@echo off
rem Run THIS repository's vaspilot even when another vaspilot package is
rem installed globally on the machine.
set "VASPILOT_REPO=%~dp0.."
set "PYTHONPATH=%VASPILOT_REPO%\src;%PYTHONPATH%"
py -3.12 -m vaspilot %*
exit /b %ERRORLEVEL%
