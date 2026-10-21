@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul
set "PS=powershell"
where powershell >nul 2>&1
if not "!ERRORLEVEL!"=="0" (
    where pwsh >nul 2>&1
    if not "!ERRORLEVEL!"=="0" goto :fail_prereq
    set "PS=pwsh"
)

rem ============================================================
rem  PhotoCollect 单文件构建流水线（版本号自动读取 con.py）
rem  1 完整测试  2 源码验收  3 onefile 干净构建
rem  4 EXE 验收 + OCR  5 原子发布到 dist 和桌面
rem  不生成 onedir、快捷方式或便携 ZIP。
rem  新 EXE 完整验收前不会替换桌面旧版。
rem ============================================================

set "OUT=outputs"
set "LOG=%OUT%\release.log"
set "RESULT=%OUT%\release_result.txt"

if not exist "%OUT%" mkdir "%OUT%"
if not exist "%OUT%" goto :fail_prereq
if not exist "con.py" goto :fail_prereq
set "VERSION="
for /f "delims=" %%I in ('%PS% -NoProfile -Command "$m=Select-String -LiteralPath 'con.py' -Pattern 'APP_VERSION\s*=\s*\x22([0-9]+\.[0-9]+\.[0-9]+)\x22' | Select-Object -First 1; if($null -ne $m){$m.Matches[0].Groups[1].Value}"') do set "VERSION=%%I"
if not defined VERSION goto :fail_prereq
if /I "%PHOTOCOLLECT_VERSION_ONLY%"=="1" (
    echo %VERSION%
    exit /b 0
)

set "LOG=%OUT%\release-v%VERSION%.log"
set "RESULT=%OUT%\release-v%VERSION%-result.txt"
set "FIXTURE=tests\fixtures\container_sample.jpg"
set "BUILD_DIST=%OUT%\singlefile-dist-v%VERSION%"
set "BUILD_WORK=%OUT%\singlefile-work-v%VERSION%"
set "SOURCE_WORK=%OUT%\acceptance-source-v%VERSION%_workspace"
set "EXE_WORK=%OUT%\acceptance-exe-v%VERSION%_workspace"
set "NEWEXE=%BUILD_DIST%\PhotoCollect.exe"
set "DISTEXE=dist\PhotoCollect.exe"
set "DIST_NEW=dist\PhotoCollect.exe.new"
set "BACKUP_DIR=%CD%\.safety-backups"

if exist "%RESULT%" del /q "%RESULT%" >nul 2>&1

set "PY=python"
where python >nul 2>&1
if not "!ERRORLEVEL!"=="0" (
    where py >nul 2>&1
    if not "!ERRORLEVEL!"=="0" goto :fail_prereq
    set "PY=py -3"
)
if not exist "%FIXTURE%" goto :fail_prereq
if not exist "PhotoCollect.spec" goto :fail_prereq
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
if not exist "%BACKUP_DIR%" goto :fail_prereq

for /f "delims=" %%I in ('%PS% -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"') do set "DESKTOP=%%I"
for /f %%I in ('%PS% -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "TS=%%I"
if not defined DESKTOP goto :fail_prereq
if not defined TS goto :fail_prereq
set "TARGET=!DESKTOP!\PhotoCollect.exe"
set "TARGET_NEW=!DESKTOP!\PhotoCollect.exe.new"
set "OLD_SHORTCUT=!DESKTOP!\PhotoCollect.lnk"
set "PACKAGE_REPORT=%OUT%\acceptance-v%VERSION%-package-!TS!.json"

> "%LOG%" echo ==== PhotoCollect v%VERSION% single-file release ====
%PY% --version >> "%LOG%" 2>&1
echo Desktop: !DESKTOP!>> "%LOG%"

rem Fail before the expensive test/build stages when a running GUI would keep
rem the desktop executable locked. The process can still start later, so the
rem atomic deployment checks below remain necessary.
if /I not "%PHOTOCOLLECT_SKIP_DESKTOP_DEPLOY%"=="1" (
    tasklist /FI "IMAGENAME eq PhotoCollect.exe" /NH 2>nul | find /I "PhotoCollect.exe" >nul
    if not errorlevel 1 goto :fail_desktop_running
)

echo.
echo [1/5] 完整自动测试 full test suite ...
%PY% -c "import rapidocr_onnxruntime" >> "%LOG%" 2>&1
if not "!ERRORLEVEL!"=="0" goto :fail_tests
%PY% -m unittest discover -s tests -v >> "%LOG%" 2>&1
if not "!ERRORLEVEL!"=="0" goto :fail_tests
rem unittest discovery silently skips pytest-style function tests.
%PY% -m pytest tests\test_authorization_service.py -v -p no:cacheprovider >> "%LOG%" 2>&1
if not "!ERRORLEVEL!"=="0" goto :fail_tests
echo       PASS

echo.
echo [2/5] 源码隔离验收 source acceptance + fixed OCR fixture ...
if exist "%SOURCE_WORK%" rd /s /q "%SOURCE_WORK%"
if exist "%SOURCE_WORK%" goto :fail_accept
%PY% con.py --acceptance-smoke ^
    --acceptance-output "%OUT%\acceptance-source-v%VERSION%.json" ^
    --acceptance-workspace "%SOURCE_WORK%" ^
    --ocr-fixture "%FIXTURE%" >> "%LOG%" 2>&1
if not "!ERRORLEVEL!"=="0" goto :fail_accept
if exist "%SOURCE_WORK%" rd /s /q "%SOURCE_WORK%"
if exist "%SOURCE_WORK%" goto :fail_accept
echo       PASS

echo.
echo [3/5] 单文件干净构建 PyInstaller onefile clean build ...
if exist "%BUILD_DIST%" rd /s /q "%BUILD_DIST%"
if exist "%BUILD_WORK%" rd /s /q "%BUILD_WORK%"
if exist "%BUILD_DIST%" goto :fail_build
if exist "%BUILD_WORK%" goto :fail_build
%PY% -m PyInstaller PhotoCollect.spec --noconfirm --clean ^
    --distpath "%BUILD_DIST%" --workpath "%BUILD_WORK%" >> "%LOG%" 2>&1
if not "!ERRORLEVEL!"=="0" goto :fail_build
if not exist "%NEWEXE%" goto :fail_build
echo       PASS  %NEWEXE%

echo.
echo [4/5] 单文件 EXE 隔离验收 packaged acceptance + exact OCR smoke ...
if exist "%EXE_WORK%" rd /s /q "%EXE_WORK%"
if exist "%EXE_WORK%" goto :fail_exe
"%NEWEXE%" --acceptance-smoke ^
    --acceptance-output "%OUT%\acceptance-exe-v%VERSION%.json" ^
    --acceptance-workspace "%EXE_WORK%" ^
    --ocr-fixture "%FIXTURE%" >> "%LOG%" 2>&1
if not "!ERRORLEVEL!"=="0" goto :fail_exe
"%NEWEXE%" --ocr-smoke "%FIXTURE%" ^
    --ocr-output "%OUT%\ocr-exe-v%VERSION%.json" >> "%LOG%" 2>&1
if not "!ERRORLEVEL!"=="0" goto :fail_exe
%PS% -NoProfile -Command ^
    "$a=Get-Content -Raw '%OUT%\acceptance-exe-v%VERSION%.json'|ConvertFrom-Json;" ^
    "$o=Get-Content -Raw '%OUT%\ocr-exe-v%VERSION%.json'|ConvertFrom-Json;" ^
    "if(-not $a.ok -or -not $o.ok -or $o.container_no -ne 'LYGU3043781' -or $o.size_type -ne '22G1' -or -not $o.check_digit_valid){exit 1}" >> "%LOG%" 2>&1
if not "!ERRORLEVEL!"=="0" goto :fail_exe
if exist "%EXE_WORK%" rd /s /q "%EXE_WORK%"
if exist "%EXE_WORK%" goto :fail_exe
echo       PASS

echo.
echo [5/5] 原子发布 publish verified single EXE ...
if not exist "dist" mkdir "dist"
if not exist "dist" goto :fail_publish
if exist "%DIST_NEW%" del /q "%DIST_NEW%" >nul 2>&1
if exist "%DIST_NEW%" goto :fail_publish
copy /b /y "%NEWEXE%" "%DIST_NEW%" >nul
if not "!ERRORLEVEL!"=="0" goto :fail_publish
fc /b "%NEWEXE%" "%DIST_NEW%" >nul
if not "!ERRORLEVEL!"=="0" goto :fail_publish

set "DIST_BACKUP="
if exist "%DISTEXE%" (
    set "DIST_BACKUP=%BACKUP_DIR%\PhotoCollect_dist_pre_v%VERSION%_!TS!.exe"
    move /y "%DISTEXE%" "!DIST_BACKUP!" >nul
    if not "!ERRORLEVEL!"=="0" goto :fail_publish
)
move /y "%DIST_NEW%" "%DISTEXE%" >nul
if not "!ERRORLEVEL!"=="0" (
    if defined DIST_BACKUP move /y "!DIST_BACKUP!" "%DISTEXE%" >nul
    goto :fail_publish
)
fc /b "%NEWEXE%" "%DISTEXE%" >nul
if not "!ERRORLEVEL!"=="0" (
    del /q "%DISTEXE%" >nul 2>&1
    if defined DIST_BACKUP move /y "!DIST_BACKUP!" "%DISTEXE%" >nul
    goto :fail_publish
)

if /I "%PHOTOCOLLECT_SKIP_DESKTOP_DEPLOY%"=="1" goto :desktop_deploy_done

if exist "!TARGET_NEW!" del /q "!TARGET_NEW!" >nul 2>&1
if exist "!TARGET_NEW!" goto :fail_deploy_busy
copy /b /y "%DISTEXE%" "!TARGET_NEW!" >nul
if not "!ERRORLEVEL!"=="0" goto :fail_deploy
fc /b "%DISTEXE%" "!TARGET_NEW!" >nul
if not "!ERRORLEVEL!"=="0" goto :fail_deploy

set "DESKTOP_BACKUP="
if exist "!TARGET!" (
    set "DESKTOP_BACKUP=%BACKUP_DIR%\PhotoCollect_desktop_pre_v%VERSION%_!TS!.exe"
    move /y "!TARGET!" "!DESKTOP_BACKUP!" >nul
    if not "!ERRORLEVEL!"=="0" goto :fail_deploy_busy
)
move /y "!TARGET_NEW!" "!TARGET!" >nul
if not "!ERRORLEVEL!"=="0" (
    if defined DESKTOP_BACKUP move /y "!DESKTOP_BACKUP!" "!TARGET!" >nul
    goto :fail_deploy
)
fc /b "%DISTEXE%" "!TARGET!" >nul
if not "!ERRORLEVEL!"=="0" (
    del /q "!TARGET!" >nul 2>&1
    if defined DESKTOP_BACKUP move /y "!DESKTOP_BACKUP!" "!TARGET!" >nul
    goto :fail_deploy
)

if exist "!OLD_SHORTCUT!" (
    move /y "!OLD_SHORTCUT!" "%BACKUP_DIR%\PhotoCollect_onedir_shortcut_retired_!TS!.lnk" >nul
    if not "!ERRORLEVEL!"=="0" goto :fail_cleanup
)

:desktop_deploy_done

rem The original acceptance report points at the temporary build directory.
rem Bind a new immutable report to the verified dist EXE before cleanup so the
rem Synology publisher can prove it is publishing the exact accepted binary.
%PS% -NoProfile -Command ^
    "$source=Get-Content -LiteralPath '%OUT%\acceptance-exe-v%VERSION%.json' -Raw -Encoding UTF8|ConvertFrom-Json;" ^
    "$exe=(Resolve-Path -LiteralPath '%DISTEXE%').Path;" ^
    "$item=Get-Item -LiteralPath $exe;" ^
    "$sha=[Security.Cryptography.SHA256]::Create();$stream=[IO.File]::OpenRead($exe);try{$hash=([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}finally{$stream.Dispose();$sha.Dispose()};" ^
    "if(-not $source.ok -or [string]$source.version -ne '%VERSION%' -or [int64]$source.runtime.size_bytes -ne [int64]$item.Length -or ([string]$source.runtime.sha256).ToLowerInvariant() -ne $hash){exit 1};" ^
    "$source.runtime.executable=$exe;$source.runtime.size_bytes=[int64]$item.Length;$source.runtime.sha256=$hash;" ^
    "$json=$source|ConvertTo-Json -Depth 20;" ^
    "$tmp='%PACKAGE_REPORT%.tmp';[IO.File]::WriteAllText($tmp,$json,(New-Object Text.UTF8Encoding($false)));Move-Item -LiteralPath $tmp -Destination '%PACKAGE_REPORT%' -Force" >> "%LOG%" 2>&1
if not "!ERRORLEVEL!"=="0" goto :fail_publish

if exist "dist\PhotoCollect" rd /s /q "dist\PhotoCollect"
if exist "dist\PhotoCollect" goto :fail_cleanup
if exist "dist\PhotoCollect_v5.14_win64_portable.zip" del /q "dist\PhotoCollect_v5.14_win64_portable.zip" >nul 2>&1
if exist "dist\PhotoCollect_v5.14_win64_portable.zip" goto :fail_cleanup
if exist "%BUILD_WORK%" rd /s /q "%BUILD_WORK%"
if exist "%BUILD_DIST%" rd /s /q "%BUILD_DIST%"
if exist "%BUILD_WORK%" goto :fail_cleanup
if exist "%BUILD_DIST%" goto :fail_cleanup

certutil -hashfile "%DISTEXE%" SHA256 >> "%LOG%" 2>&1
if /I not "%PHOTOCOLLECT_SKIP_DESKTOP_DEPLOY%"=="1" certutil -hashfile "!TARGET!" SHA256 >> "%LOG%" 2>&1
> "%RESULT%" echo ALL_DONE_SINGLEFILE_V%VERSION%
echo.
echo ==================================================
echo   SUCCESS - v%VERSION% 单文件已测试、验收并生成
echo   构建产物  %CD%\%DISTEXE%
if /I "%PHOTOCOLLECT_SKIP_DESKTOP_DEPLOY%"=="1" goto :success_desktop_skipped
echo   桌面入口  !TARGET!
goto :success_desktop_done
:success_desktop_skipped
echo   桌面入口  已按要求跳过，现有版本未改动
:success_desktop_done
echo   验收报告  %CD%\%PACKAGE_REPORT%
echo   详细日志  %CD%\%LOG%
echo   不含 onedir、快捷方式或便携 ZIP
echo ==================================================
call :maybe_pause
exit /b 0

:fail_prereq
> "%RESULT%" echo FAILED_PREREQUISITES
echo RESULT: 发布前置条件不完整，请检查 Python、固定样图和项目文件。
call :maybe_pause
exit /b 1

:fail_desktop_running
> "%RESULT%" echo FAILED_DESKTOP_RUNNING
echo RESULT: 检测到 PhotoCollect.exe 正在运行。
echo         请先关闭软件，然后重新双击发布脚本。
call :maybe_pause
exit /b 1

:fail_tests
> "%RESULT%" echo FAILED_STAGE1_TESTS
echo RESULT: 完整自动测试未通过 - %LOG%
call :maybe_pause
exit /b 1

:fail_accept
> "%RESULT%" echo FAILED_STAGE2_ACCEPTANCE
echo RESULT: 源码隔离验收未通过 - %OUT%\acceptance-source-v%VERSION%.json
call :maybe_pause
exit /b 1

:fail_build
> "%RESULT%" echo FAILED_STAGE3_BUILD
echo RESULT: 单文件构建失败 - %LOG%
call :maybe_pause
exit /b 1

:fail_exe
> "%RESULT%" echo FAILED_STAGE4_EXE
echo RESULT: 单文件 EXE 验收或 OCR 回归未通过 - %LOG%
call :maybe_pause
exit /b 1

:fail_publish
if exist "%DIST_NEW%" del /q "%DIST_NEW%" >nul 2>&1
> "%RESULT%" echo FAILED_STAGE5_PUBLISH
echo RESULT: dist 单文件发布失败，已验收构建仍保留在 %NEWEXE%
call :maybe_pause
exit /b 1

:fail_deploy_busy
if exist "!TARGET_NEW!" del /q "!TARGET_NEW!" >nul 2>&1
> "%RESULT%" echo FAILED_STAGE5_DESKTOP_BUSY
echo RESULT: 桌面旧版可能仍在运行。关闭 PhotoCollect 后重新执行。
call :maybe_pause
exit /b 1

:fail_deploy
if exist "!TARGET_NEW!" del /q "!TARGET_NEW!" >nul 2>&1
> "%RESULT%" echo FAILED_STAGE5_DEPLOY
echo RESULT: 桌面单文件部署失败；如有旧版，脚本已尝试恢复。
call :maybe_pause
exit /b 1

:fail_cleanup
> "%RESULT%" echo FAILED_STAGE5_CLEANUP
echo RESULT: 新单文件已部署，但旧目录版入口或临时构建物未完全清理。
call :maybe_pause
exit /b 1

:maybe_pause
if /I not "%PHOTOCOLLECT_NO_PAUSE%"=="1" pause
exit /b 0
