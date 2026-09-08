@echo off
setlocal EnableDelayedExpansion
rem VaspLoom unified web console launcher (ASCII-only: cmd.exe parses
rem batch files in the ANSI codepage, so Chinese here would break)
set "UILOG=%USERPROFILE%\.vasploom\ui.log"

curl -s -o nul http://127.0.0.1:8930/healthz 2>nul
if not errorlevel 1 (
  echo VaspLoom UI is already running on port 8930.
  set "UIURL="
  if exist "%UILOG%" (
    for /f "usebackq tokens=*" %%i in (`findstr /c:"VaspLoom UI ready:" "%UILOG%"`) do set "UILINE=%%i"
    if defined UILINE set "UIURL=!UILINE:*ready: =!"
  )
  if defined UIURL (
    start "" !UIURL!
    echo Reopened your session tab: !UIURL!
  ) else (
    start "" http://127.0.0.1:8930/
    echo Opened the landing page. To recover the session URL, run vasploom ui
    echo in a terminal, or restart it: close the minimized VaspLoom UI window
    echo first, then run this shortcut again.
  )
  ping -n 7 127.0.0.1 >nul
  exit /b 0
)

py -3.12 -m vasploom ui %*
exit /b %ERRORLEVEL%
