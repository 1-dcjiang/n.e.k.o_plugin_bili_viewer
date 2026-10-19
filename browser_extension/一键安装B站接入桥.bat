@echo off
chcp 65001 >nul
title B站接入桥 (bili-bridge) 一键安装
echo ============================================================
echo      B站接入桥 (bili-bridge) — 一键安装 / 更新
echo ============================================================
echo.

setlocal
set "EXT_SRC=%~dp0bili-bridge"
set "EXT_DST=%USERPROFILE%\Documents\bili-bridge-extension"

if not exist "%EXT_SRC%\manifest.json" (
    echo [错误] 找不到扩展文件。
    echo        请确认 "bili-bridge" 文件夹与本脚本在同一目录，然后重新运行。
    echo.
    pause
    exit /b 1
)

if exist "%EXT_DST%" (
    echo [提示] 检测到已安装的扩展，正在覆盖更新...
    rmdir /s /q "%EXT_DST%"
)
xcopy "%EXT_SRC%" "%EXT_DST%" /e /i /q >nul
if errorlevel 1 (
    echo [错误] 复制扩展文件失败。
    pause
    exit /b 1
)
echo [完成] 扩展已就绪：%EXT_DST%
echo.

echo 下一步（最后一步手动，约 10 秒）：
echo   1. 已为你打开 chrome://extensions 页面
echo   2. 打开页面右上角「开发者模式」开关
echo   3. 点「加载已解压的扩展程序」，选择上面的目录
echo.
echo 安装后：扩展会随浏览器自动运行，把 B站登录态实时同步给 YUI。
echo （如果扩展图标显示灰色，点一下扩展图标选「允许」即可。）
echo.

start "" chrome "chrome://extensions" 2>nul
if errorlevel 1 start "" msedge "chrome://extensions" 2>nul
if errorlevel 1 start "" "https://chrome.google.com/webstore" 2>nul

echo.
pause
