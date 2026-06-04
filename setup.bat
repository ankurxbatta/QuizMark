@echo off
REM ═══════════════════════════════════════════════════════════════════════
REM  QuizMark – First-run setup  (Windows)
REM  Usage: Double-click setup.bat  OR  run from Command Prompt
REM ═══════════════════════════════════════════════════════════════════════
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   ╔══════════════════════════════════════════╗
echo   ║          QuizMark  —  Setup              ║
echo   ╚══════════════════════════════════════════╝
echo.

REM ── 1. Prerequisites ──────────────────────────────────────────────────────
echo   → Checking prerequisites...
docker version >nul 2>&1
if errorlevel 1 (
    echo   ✗  Docker is not running or not installed.
    echo      Install Docker Desktop: https://docs.docker.com/get-docker/
    pause & exit /b 1
)

docker compose version >nul 2>&1
if not errorlevel 1 ( set DC=docker compose ) else (
    docker-compose version >nul 2>&1
    if not errorlevel 1 ( set DC=docker-compose ) else (
        echo   ✗  Docker Compose not found. Update Docker Desktop.
        pause & exit /b 1
    )
)
echo   ✓  Docker and Compose ready

REM ── 2. .env ───────────────────────────────────────────────────────────────
echo   → Checking .env...
if not exist .env (
    if not exist .env.example ( echo   ✗  .env.example not found. Re-clone the repo. & pause & exit /b 1 )
    copy .env.example .env >nul
    echo   ✓  .env created from .env.example
)

call :read_env SECRET_KEY ""
call :read_env ADMIN_PASSWORD ""
call :read_env ADMIN_USERNAME "admin"
call :read_env GEMINI_API_KEY ""
call :read_env OPENAI_API_KEY ""
call :read_env ANTHROPIC_API_KEY ""

REM SECRET_KEY
if "!SECRET_KEY!"=="" goto :gen_sk
if "!SECRET_KEY:~0,7!"=="REPLACE" goto :gen_sk
goto :after_sk
:gen_sk
for /f "delims=" %%i in ('python -c "import secrets; print(secrets.token_hex(32))" 2^>nul') do set GENERATED_SK=%%i
if "!GENERATED_SK!"=="" for /f "delims=" %%i in ('powershell -Command "[Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower()"') do set GENERATED_SK=%%i
call :set_env SECRET_KEY "!GENERATED_SK!"
echo   ✓  SECRET_KEY generated
:after_sk

REM ADMIN_PASSWORD
if "!ADMIN_PASSWORD!"=="" goto :need_pw
if "!ADMIN_PASSWORD:~0,7!"=="REPLACE" goto :need_pw
goto :after_pw
:need_pw
echo.
echo   ⚠  Choose an admin password for the instructor login.
set /p ADMIN_PW_INPUT="  Admin password (min 8 chars): "
if "!ADMIN_PW_INPUT!"=="" ( echo   ✗  Password cannot be empty. & pause & exit /b 1 )
call :set_env ADMIN_PASSWORD "!ADMIN_PW_INPUT!"
echo   ✓  ADMIN_PASSWORD saved
:after_pw

REM GEMINI_API_KEY
if "!GEMINI_API_KEY!"=="" goto :need_gemini
if "!GEMINI_API_KEY:~0,7!"=="REPLACE" goto :need_gemini
goto :after_gemini
:need_gemini
echo.
echo   ⚠  GEMINI_API_KEY — used for vector embeddings (free).
echo      Get a free key: https://aistudio.google.com/app/apikey
set /p GK_INPUT="  Paste Gemini API key: "
if "!GK_INPUT!"=="" ( echo   ✗  Cannot be empty. & pause & exit /b 1 )
call :set_env GEMINI_API_KEY "!GK_INPUT!"
echo   ✓  GEMINI_API_KEY saved
:after_gemini

REM OPENAI_API_KEY
if "!OPENAI_API_KEY!"=="" goto :need_openai
if "!OPENAI_API_KEY:~0,7!"=="REPLACE" goto :need_openai
goto :after_openai
:need_openai
echo.
echo   ⚠  OPENAI_API_KEY — primary provider (vision, math, generation, marking).
echo      Get a key: https://platform.openai.com/api-keys
set /p OK_INPUT="  Paste OpenAI API key: "
if "!OK_INPUT!"=="" ( echo   ✗  Cannot be empty. & pause & exit /b 1 )
call :set_env OPENAI_API_KEY "!OK_INPUT!"
echo   ✓  OPENAI_API_KEY saved
:after_openai

REM ANTHROPIC_API_KEY
if "!ANTHROPIC_API_KEY!"=="" goto :need_anthropic
if "!ANTHROPIC_API_KEY:~0,7!"=="REPLACE" goto :need_anthropic
goto :after_anthropic
:need_anthropic
echo.
echo   ⚠  ANTHROPIC_API_KEY — fallback (activates when OpenAI hits quota).
echo      Get a key: https://console.anthropic.com
set /p AK_INPUT="  Paste Anthropic API key: "
if "!AK_INPUT!"=="" ( echo   ✗  Cannot be empty. & pause & exit /b 1 )
call :set_env ANTHROPIC_API_KEY "!AK_INPUT!"
echo   ✓  ANTHROPIC_API_KEY saved
:after_anthropic
echo   ✓  .env is ready

REM ── 3. Directories ────────────────────────────────────────────────────────
echo   → Creating required directories...
if not exist "data\uploads" mkdir "data\uploads"
if not exist "Book" mkdir "Book"
echo   ✓  Directories ready

REM ── 4. Build ──────────────────────────────────────────────────────────────
echo   → Building Docker images (first run takes 5-10 minutes)...
%DC% build
if errorlevel 1 ( echo   ✗  Build failed. Check output above. & pause & exit /b 1 )
echo   ✓  Images built

REM ── 5. Start ──────────────────────────────────────────────────────────────
echo   → Starting all services...
%DC% up -d
if errorlevel 1 ( echo   ✗  Failed to start. & pause & exit /b 1 )
echo   ✓  Containers started

REM ── 6. Health checks ──────────────────────────────────────────────────────
echo   → Waiting for backend (up to 3 min)...
set TRIES=0
:wait_backend
set /a TRIES+=1
if %TRIES% gtr 90 ( echo   ✗  Timed out. Run: %DC% logs backend & pause & exit /b 1 )
curl -sf http://localhost:8000/health >nul 2>&1
if errorlevel 1 ( timeout /t 2 /nobreak >nul & goto :wait_backend )
echo   ✓  Backend healthy

echo   → Waiting for frontend...
set TRIES=0
:wait_frontend
set /a TRIES+=1
if %TRIES% gtr 90 ( echo   ⚠  Frontend still starting — try http://localhost:3000 shortly. & goto :done )
curl -sf http://localhost:3000 >nul 2>&1
if errorlevel 1 ( timeout /t 2 /nobreak >nul & goto :wait_frontend )
echo   ✓  Frontend healthy

REM ── 7. Done ───────────────────────────────────────────────────────────────
:done
echo.
echo   ╔══════════════════════════════════════════════════╗
echo   ║   ✓  QuizMark is up and running!                 ║
echo   ╚══════════════════════════════════════════════════╝
echo.
echo   App            -^>  http://localhost:3000
echo   API docs       -^>  http://localhost:8000/docs
echo   MongoDB UI     -^>  http://localhost:8081
echo   Worker monitor -^>  http://localhost:5555  (Flower)
echo.
echo   Login: username=!ADMIN_USERNAME!  + the password you set above
echo.
echo   Quick commands:
echo     Stop all:   %DC% down
echo     Restart:    %DC% up -d
echo     Logs:       %DC% logs -f
echo     Rebuild:    %DC% up -d --build
echo     Full reset: %DC% down -v   (deletes ALL data)
echo.
echo   Getting started:
echo     1. Log in at http://localhost:3000
echo     2. Add Book -^> upload a PDF textbook (up to 25 MB, 700 pages)
echo     3. Wait for ingestion -^> Library -^> book -^> Generate Questions
echo.
pause
exit /b 0

REM ── Subroutines ───────────────────────────────────────────────────────────
:read_env
set "%~1=%~2"
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "_k=%%A"
    set "_k=!_k: =!"
    if /i "!_k!"=="%~1" set "%~1=%%B"
)
exit /b 0

:set_env
set "_tk=%~1"
set "_tv=%~2"
set "_found=0"
set "_tmp=%TEMP%\qm_env.txt"
if exist "!_tmp!" del "!_tmp!"
for /f "usebackq delims=" %%L in (".env") do (
    set "_line=%%L"
    for /f "tokens=1 delims==" %%K in ("!_line!") do (
        if /i "%%K"=="!_tk!" (
            echo !_tk!=!_tv!>> "!_tmp!"
            set "_found=1"
        ) else (
            echo %%L>> "!_tmp!"
        )
    )
)
if "!_found!"=="0" echo !_tk!=!_tv!>> "!_tmp!"
move /y "!_tmp!" ".env" >nul
exit /b 0
