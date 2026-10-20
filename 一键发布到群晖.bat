@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ==================================================
echo   PhotoCollect Synology Publisher
echo ==================================================
echo.
echo First use: copy the publisher token before continuing.
echo Windows encrypts it for the current user; later runs reuse it.
echo Without -Yes, type PUBLISH after checking the release details.
echo.

where pwsh >nul 2>&1
if errorlevel 1 goto :missing_pwsh

pwsh -NoProfile -Command "if ($PSVersionTable.PSVersion.Major -lt 7) { exit 1 }"
if errorlevel 1 goto :missing_pwsh

pwsh -NoProfile -File "%~dp0publish_to_synology.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
goto :done

:missing_pwsh
echo ERROR: PowerShell 7 or later is required ^(pwsh^).
set "EXIT_CODE=1"

:done
echo.
if "%EXIT_CODE%"=="0" (
    echo Publish workflow completed.
) else (
    echo Publish workflow stopped. Exit code: %EXIT_CODE%
)
if /I not "%PHOTOCOLLECT_NO_PAUSE%"=="1" pause
exit /b %EXIT_CODE%
