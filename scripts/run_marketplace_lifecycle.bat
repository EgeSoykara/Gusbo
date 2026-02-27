@echo off
setlocal

set "ROOT_DIR=%~dp0.."
pushd "%ROOT_DIR%" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Could not switch to project directory: %ROOT_DIR%
    exit /b 1
)

if defined PYTHON_EXE (
    set "PYTHON_CMD=%PYTHON_EXE%"
) else (
    set "PYTHON_CMD=python"
)

%PYTHON_CMD% manage.py marketplace_lifecycle %*
set "EXIT_CODE=%ERRORLEVEL%"

popd >nul 2>&1
exit /b %EXIT_CODE%
