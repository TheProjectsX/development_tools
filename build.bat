@echo off
setlocal EnableExtensions EnableDelayedExpansion

echo ================================================
echo   Interactive Python Project EXE Builder
echo ================================================
echo.

set "ROOT_DIR=%~dp0"
pushd "%ROOT_DIR%" >nul

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org
    popd >nul
    pause
    exit /b 1
)

:: Check pip
pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip not found. Ensure pip is installed with Python.
    popd >nul
    pause
    exit /b 1
)

:: Install PyInstaller if not present
echo [1/6] Checking PyInstaller...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller.
        popd >nul
        pause
        exit /b 1
    )
)

echo.
echo [2/6] Scanning buildable projects...
set /a COUNT=0

for /d %%D in (*) do (
    set "PROJECT_DIR=%%~fD"
    set "MAIN_SCRIPT="

    if exist "%%D\app.py" set "MAIN_SCRIPT=%%D\app.py"
    if not defined MAIN_SCRIPT if exist "%%D\%%D.py" set "MAIN_SCRIPT=%%D\%%D.py"

    if not defined MAIN_SCRIPT (
        for %%F in ("%%D\*.py") do (
            if /I not "%%~nxF"=="__init__.py" if /I not "%%~xF"==".spec" if not defined MAIN_SCRIPT set "MAIN_SCRIPT=%%~fF"
        )
    )

    if defined MAIN_SCRIPT (
        set /a COUNT+=1
        set "P_NAME_!COUNT!=%%D"
        set "P_DIR_!COUNT!=%%~fD"
        set "P_MAIN_!COUNT!=!MAIN_SCRIPT!"
    )
)

if %COUNT% EQU 0 (
    echo [ERROR] No buildable Python projects were found under:
    echo         %ROOT_DIR%
    popd >nul
    pause
    exit /b 1
)

echo.
echo Buildable projects:
for /L %%I in (1,1,%COUNT%) do (
    call echo   %%I^) %%P_NAME_%%I%%  [main: %%P_MAIN_%%I%%]
)
echo.

set "CHOICE="
set /p CHOICE=Choose project number (1-%COUNT%): 

if not defined CHOICE (
    echo [ERROR] No selection made.
    popd >nul
    pause
    exit /b 1
)

set "NOT_NUM="
for /f "delims=0123456789" %%A in ("%CHOICE%") do set "NOT_NUM=%%A"
if defined NOT_NUM (
    echo [ERROR] Invalid selection: %CHOICE%
    popd >nul
    pause
    exit /b 1
)

if %CHOICE% LSS 1 (
    echo [ERROR] Selection out of range.
    popd >nul
    pause
    exit /b 1
)
if %CHOICE% GTR %COUNT% (
    echo [ERROR] Selection out of range.
    popd >nul
    pause
    exit /b 1
)

call set "SELECTED_NAME=%%P_NAME_%CHOICE%%%"
call set "SELECTED_DIR=%%P_DIR_%CHOICE%%%"
call set "SELECTED_MAIN=%%P_MAIN_%CHOICE%%%"

echo.
echo Selected project: %SELECTED_NAME%
echo Main script:      %SELECTED_MAIN%

set "EXE_NAME=%SELECTED_NAME%"
set /p EXE_NAME=EXE name (default: %SELECTED_NAME%): 
if not defined EXE_NAME set "EXE_NAME=%SELECTED_NAME%"

echo.
echo Build mode:
echo   1^) GUI app ^(no cmd window^)
echo   2^) Console app ^(cmd window visible^)
set "MODE_CHOICE="
set /p MODE_CHOICE=Choose build mode [1-2] (default: 1): 
if not defined MODE_CHOICE set "MODE_CHOICE=1"

set "PYI_MODE="
set "MODE_NOT_NUM="
for /f "delims=0123456789" %%A in ("%MODE_CHOICE%") do set "MODE_NOT_NUM=%%A"
if defined MODE_NOT_NUM (
    echo [ERROR] Invalid build mode: %MODE_CHOICE%
    popd >nul
    pause
    exit /b 1
)
if "%MODE_CHOICE%"=="1" set "PYI_MODE=--windowed"
if "%MODE_CHOICE%"=="2" set "PYI_MODE="
if not "%MODE_CHOICE%"=="1" if not "%MODE_CHOICE%"=="2" (
    echo [ERROR] Invalid build mode: %MODE_CHOICE%
    popd >nul
    pause
    exit /b 1
)

set "DIST_DIR=%SELECTED_DIR%\dist"
set "WORK_DIR=%SELECTED_DIR%\build"
set "SPEC_DIR=%SELECTED_DIR%"
set "FINAL_EXE=%DIST_DIR%\%EXE_NAME%.exe"

echo.
echo [3/6] Creating dist folder...
if not exist "%DIST_DIR%" mkdir "%DIST_DIR%"

echo [4/6] Building EXE...
pyinstaller --noconfirm --clean --onefile %PYI_MODE% --name "%EXE_NAME%" --distpath "%DIST_DIR%" --workpath "%WORK_DIR%" --specpath "%SPEC_DIR%" "%SELECTED_MAIN%"
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. Review PyInstaller output above.
    popd >nul
    pause
    exit /b 1
)

echo.
echo [5/6] Build completed.
if exist "%FINAL_EXE%" (
    echo [6/6] Done!
    echo SUCCESS: EXE created at:
    echo %FINAL_EXE%
) else (
    echo [6/6] Done with warnings.
    echo [WARN] Build finished but EXE was not found at expected path:
    echo %FINAL_EXE%
)

popd >nul
echo.
pause
