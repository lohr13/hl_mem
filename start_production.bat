@echo off
setlocal
set "ROOT_DIR=%~dp0"

if not exist "%ROOT_DIR%hl_mem.toml" (
    echo Missing configuration file: "%ROOT_DIR%hl_mem.toml" 1>&2
    exit /b 1
)

call "%ROOT_DIR%scripts\hlmem-python.cmd" "%ROOT_DIR%start_server.py"
set "EXIT_CODE=%ERRORLEVEL%"
exit /b %EXIT_CODE%
