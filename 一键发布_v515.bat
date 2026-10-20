@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

rem Stable double-click entry for the version-independent build pipeline.
rem Keep this launcher ASCII-only so it works regardless of the active code page.
set "BUILD_SCRIPT="
for %%F in ("%~dp0*PhotoCollect.bat") do (
    if exist "%%~fF" set "BUILD_SCRIPT=%%~fF"
)

if not defined BUILD_SCRIPT (
    echo.
    echo ERROR: The PhotoCollect build script was not found.
    echo Folder: %~dp0
    set "EXIT_CODE=2"
    goto :finish
)

rem A stale inherited value must never turn a double-click build into a silent
rem version query. The child stays non-interactive; this launcher pauses once.
set "PHOTOCOLLECT_VERSION_ONLY="
set "PHOTOCOLLECT_NO_PAUSE=1"

echo ==================================================
echo   PhotoCollect build and verification
echo ==================================================
echo Build script: %BUILD_SCRIPT%
echo.

call "%BUILD_SCRIPT%"
set "EXIT_CODE=%ERRORLEVEL%"

:finish
echo.
if "%EXIT_CODE%"=="0" (
    echo SUCCESS: Build workflow completed.
) else (
    echo FAILED: Build workflow stopped. Exit code: %EXIT_CODE%
    echo Check the latest release log in: %~dp0outputs
)
echo.
pause
exit /b %EXIT_CODE%
