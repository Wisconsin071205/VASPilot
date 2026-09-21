@echo off
rem Run THIS repository's vaspilot even when another vaspilot package is
rem installed globally on the machine. Prefers the project virtualenv; falls
rem back to whatever `python` resolves to (the Store `py -3.12` stub does not
rem work here, so it is deliberately not used).
setlocal
set "VASPILOT_REPO=%~dp0.."
set "PYTHONPATH=%VASPILOT_REPO%\src;%PYTHONPATH%"
if exist "%VASPILOT_REPO%\.venv\Scripts\python.exe" (
  "%VASPILOT_REPO%\.venv\Scripts\python.exe" -m vaspilot %*
) else (
  python -m vaspilot %*
)
exit /b %ERRORLEVEL%
