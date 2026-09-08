@echo off
rem Run THIS repository's vasploom even when another vasploom package is
rem installed globally on the machine.
set "VASPLOOM_REPO=%~dp0.."
set "PYTHONPATH=%VASPLOOM_REPO%\src;%PYTHONPATH%"
py -3.12 -m vasploom %*
exit /b %ERRORLEVEL%
