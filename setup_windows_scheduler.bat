@echo off
chcp 65001 > nul
title Geo_Paper_Radar V3.0 — 计划任务安装程序

echo ============================================
echo  🌍 Geo_Paper_Radar V3.0 自动推送计划任务
echo ============================================
echo.
echo  [信息] 正在配置 Windows 计划任务...
echo.

:: 获取当前目录的绝对路径
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_PATH=%SCRIPT_DIR%paper_radar.py"
set "PYTHON_PATH=python"

:: 检查 Python 是否可用
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo  [错误] 未找到 Python，请先安装 Python 并添加到 PATH
    pause
    exit /b 1
)

:: 检查脚本是否存在
if not exist "%SCRIPT_PATH%" (
    echo  [错误] 未找到 paper_radar.py，请确认脚本位于: %SCRIPT_PATH%
    pause
    exit /b 1
)

:: 创建计划任务（每天早晨 10:00，以当前用户权限运行）
schtasks /create ^
    /tn "Geo_Paper_Radar_Daily" ^
    /tr "%PYTHON_PATH% \"%SCRIPT_PATH%\"" ^
    /sc daily ^
    /st 10:00 ^
    /f ^
    /rl HIGHEST

if %ERRORLEVEL% equ 0 (
    echo.
    echo  ✅ 计划任务创建成功！
    echo.
    echo  ┌─────────────────────────────────────────────
    echo  │ 任务名称: Geo_Paper_Radar_Daily
    echo  │ 执行时间: 每天 10:00
    echo  │ 执行脚本: %SCRIPT_PATH%
    echo  │ 运行身份: 当前用户
    echo  └─────────────────────────────────────────────
    echo.
    echo  📌 若要手动运行，请在终端执行：
    echo     schtasks /run /tn Geo_Paper_Radar_Daily
    echo.
    echo  📌 若要删除任务，请执行：
    echo     schtasks /delete /tn Geo_Paper_Radar_Daily /f
) else (
    echo.
    echo  [警告] 计划任务创建失败，请尝试以管理员身份运行此脚本。
    echo         右键单击本文件 → 以管理员身份运行
)

echo.
pause