@echo off
rem Run THIS repository's rsci even when another rsci package is installed.
rem Prefer its virtualenv and avoid the Windows Store `py -3.x` launcher stub.
setlocal
set "RSCI_REPO=%~dp0.."
set "PYTHONPATH=%RSCI_REPO%\src;%PYTHONPATH%"
if exist "%RSCI_REPO%\.venv\Scripts\python.exe" (
  "%RSCI_REPO%\.venv\Scripts\python.exe" -m rsci %*
) else (
  python -m rsci %*
)
exit /b %ERRORLEVEL%
