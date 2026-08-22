@echo off
setlocal
set "PYTHONPATH="
set "PYTHONHOME="
set "VIRTUAL_ENV="
set "CONDA_PREFIX="
set "ROOT_DIR=%~dp0.."
set "PYTHON_EXE=%ROOT_DIR%\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
    echo Missing virtual environment Python: "%PYTHON_EXE%" 1>&2
    exit /b 1
)

pushd "%ROOT_DIR%" || exit /b 1
"%PYTHON_EXE%" -m hl_mem.evaluation.runtime_guard --expected-venv "%ROOT_DIR%\.venv"
if errorlevel 1 (
    popd
    exit /b 1
)
"%PYTHON_EXE%" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
