@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo Missing virtual environment Python: "%PYTHON_EXE%" 1>&2
    exit /b 1
)
if not exist "%ROOT_DIR%hl_mem.toml" (
    echo Missing configuration file: "%ROOT_DIR%hl_mem.toml" 1>&2
    exit /b 1
)

pushd "%ROOT_DIR%" || exit /b 1
"%PYTHON_EXE%" "%ROOT_DIR%start_server.py"
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
