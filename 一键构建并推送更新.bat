@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul

echo ==================================================
echo   PhotoCollect 一键构建并推送更新
echo ==================================================
echo.
echo 首次使用：运行前先复制 publisher 令牌。
echo 脚本会用 Windows 当前用户加密保存；以后只需双击本文件。
echo.
echo 将依次完成：完整测试、单文件构建、EXE 验收、
echo 部署本机新版、上传群晖、校验 HTTP、推送软件通知。
echo 任一步失败都会停止，不会改动群晖 latest.json。
echo.

set "PHOTOCOLLECT_NO_PAUSE=1"
call "%~dp0一键发布到群晖.bat" -InitializeTokenOnly
if errorlevel 1 goto :token_failed

call "%~dp0一键构建_PhotoCollect.bat"
if errorlevel 1 goto :build_failed

echo.
echo 本机构建和验收已通过。
call "%~dp0一键发布到群晖.bat" -Yes
if errorlevel 1 goto :publish_failed

echo.
echo SUCCESS: 新版本已发布，同事的软件会收到提示；离线电脑启动后也会补检。
pause
exit /b 0

:token_failed
echo.
echo FAILED: publisher 令牌未就绪，尚未开始构建或上传。
echo 首次使用请复制有效 publisher 令牌后重试。
pause
exit /b 1

:build_failed
echo.
echo FAILED: 构建或验收未通过，未开始上传群晖。
pause
exit /b 1

:publish_failed
echo.
echo FAILED: 群晖发布流程未完整成功，请查看 outputs 下最新发布记录。
pause
exit /b 1
