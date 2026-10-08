@echo off
REM Alle Qualitaets-Checks des Projekts in fester Reihenfolge:
REM   1. ruff format  (formatiert die Dateien direkt, laeuft nativ unter Windows)
REM   2. ruff check   (Lint; mit --fix werden behebbare Fehler korrigiert)
REM   3. pytest       (Testlauf im Docker-Container, siehe Dockerfile.test)
REM   4. Karte        (prettier, eslint, vitest, vite build unter card/ - nur mit Node;
REM                    ohne Node gibt es einen Hinweis, aber keinen Fehlschlag)
REM Aufruf:  check.cmd  |  check.cmd --fix
setlocal enabledelayedexpansion
pushd "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set "IMAGE=ha-delivery-tracker-test"

set "LINT_ARGS="
if /I "%~1"=="--fix" set "LINT_ARGS=--fix"

set "FAILED="

echo ================== ruff format ==================
"%PY%" -m ruff format .
if errorlevel 1 set "FAILED=1"

echo.
echo ================== ruff check ===================
"%PY%" -m ruff check %LINT_ARGS% .
if errorlevel 1 set "FAILED=1"

echo.
echo ================== pytest (docker) ==============
REM Home Assistant importiert Unix-Module und laeuft daher nicht nativ unter Windows.
REM Der erste Build dauert ein paar Minuten, danach greift der Layer-Cache.
docker build -q -t %IMAGE% -f Dockerfile.test . >nul
if errorlevel 1 (
    echo.
    echo Docker-Image konnte nicht gebaut werden - laeuft Docker Desktop?
    set "FAILED=1"
) else (
    docker run --rm -v "%CD%:/app" -w /app %IMAGE% pytest -q
    if errorlevel 1 set "FAILED=1"
)

echo.
echo ================== Karte (node) =================
set "CARD_SKIPPED="
where npm >nul 2>nul
if errorlevel 1 (
    echo Node/npm nicht gefunden - Lint, Tests und Build der Karte uebersprungen.
    set "CARD_SKIPPED=1"
) else (
    call :card
    if errorlevel 1 set "FAILED=1"
)

echo.
if defined CARD_SKIPPED echo HINWEIS: Karte nicht geprueft, Node ist nicht installiert.
if defined FAILED (
    echo ERGEBNIS: FEHLGESCHLAGEN
    popd
    endlocal
    exit /b 1
)
echo ERGEBNIS: alles gruen
popd
endlocal
exit /b 0

REM Karte: formatieren wie bei ruff, dann Lint, Tests und Build. Der Build landet
REM in custom_components\delivery_tracker\frontend\ und prueft sich selbst.
:card
set "CARD_FAILED="
pushd card
if not exist node_modules\ (
    call npm ci --no-audit --no-fund
    if errorlevel 1 (
        set "CARD_FAILED=1"
        goto card_end
    )
)
call npm run --silent format >nul
if errorlevel 1 set "CARD_FAILED=1"
if defined LINT_ARGS (call npm run --silent lint:fix) else (call npm run --silent lint)
if errorlevel 1 set "CARD_FAILED=1"
call npm run --silent test
if errorlevel 1 set "CARD_FAILED=1"
call npm run --silent build
if errorlevel 1 set "CARD_FAILED=1"
:card_end
popd
if defined CARD_FAILED exit /b 1
exit /b 0
