@echo off
setlocal enabledelayedexpansion

REM ------------------------------------------------------------
REM RetroLink - Deploy Producao (Windows)
REM Uso:
REM   deploy_prod.bat "D:\RetroLinkCompartilhado" 8000
REM ------------------------------------------------------------

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul

set "SHARED_DIR=%~1"
set "PORT=%~2"

if "%SHARED_DIR%"=="" (
    set /p SHARED_DIR=Informe a pasta compartilhada (ex: D:\RetroLinkCompartilhado): 
)

if "%PORT%"=="" set "PORT=8000"

if "%SHARED_DIR%"=="" (
    echo [ERRO] Pasta compartilhada nao informada.
    popd >nul
    exit /b 1
)

echo.
echo [1/5] Pasta compartilhada: %SHARED_DIR%
if not exist "%SHARED_DIR%" (
    mkdir "%SHARED_DIR%"
    if errorlevel 1 (
        echo [ERRO] Nao foi possivel criar a pasta compartilhada.
        popd >nul
        exit /b 1
    )
)

echo [2/5] Preparando ambiente virtual...
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 (
        python -m venv .venv
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Nao foi possivel criar o ambiente virtual.
    popd >nul
    exit /b 1
)

call ".venv\Scripts\activate.bat"

echo [3/5] Atualizando pip...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [ERRO] Falha ao atualizar o pip.
    popd >nul
    exit /b 1
)

echo [4/5] Instalando dependencias...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERRO] Falha ao instalar dependencias.
    popd >nul
    exit /b 1
)

echo [5/5] Iniciando servidor RetroLink...
set "RETROLINK_SHARED_DIR=%SHARED_DIR%"
echo.
echo RETROLINK_SHARED_DIR=%RETROLINK_SHARED_DIR%
echo Servidor em: http://0.0.0.0:%PORT%
echo.
echo Dica: as pastas extras (inclusive D:\, E:\ etc.) podem ser adicionadas no Backup do modo moderno.
echo.
python -m uvicorn main:app --host 0.0.0.0 --port %PORT%

popd >nul
endlocal
