@echo off
REM ═══════════════════════════════════════════════════════════════════════
REM  QuizMark – First-run setup  (Windows)
REM  Usage: Double-click setup.bat  OR  run from Command Prompt / PowerShell
REM ═══════════════════════════════════════════════════════════════════════
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   ╔═══════════════════════════════════════╗
echo   ║         QuizMark  –  Setup            ║
echo   ╚═══════════════════════════════════════╝
echo.

REM ── 1. Prerequisites ──────────────────────────────────────────────────────
echo   → Checking prerequisites...

docker version >nul 2>&1
if errorlevel 1 (
    echo   ✗  Docker is not running or not installed.
    echo      Install Docker Desktop: https://docs.docker.com/get-docker/
    echo      Then start Docker Desktop and run setup.bat again.
    pause
    exit /b 1
)

REM Try 'docker compose' (v2) first, fall back to 'docker-compose' (v1)
docker compose version >nul 2>&1
if not errorlevel 1 (
    set DC=docker compose
) else (
    docker-compose version >nul 2>&1
    if not errorlevel 1 (
        set DC=docker-compose
    ) else (
        echo   ✗  Docker Compose not found. Update Docker Desktop and try again.
        pause
        exit /b 1
    )
)

echo   ✓  Docker and Docker Compose are ready

REM ── 2. .env setup ─────────────────────────────────────────────────────────
echo   → Checking .env...

if not exist .env (
    if not exist .env.example (
        echo   ✗  .env.example not found. Re-clone the repository.
        pause
        exit /b 1
    )
    copy .env.example .env >nul
    echo   ✓  .env created from .env.example
)

REM Read a value from .env
call :read_env SECRET_KEY ""
call :read_env GEMINI_API_KEY ""
call :read_env ADMIN_PASSWORD ""
call :read_env ADMIN_USERNAME "admin"

REM Auto-generate SECRET_KEY if missing/placeholder
if "!SECRET_KEY!"=="" goto :gen_secret
if "!SECRET_KEY:~0,7!"=="REPLACE" goto :gen_secret
goto :after_secret

:gen_secret
echo   → Generating SECRET_KEY...
for /f "delims=" %%i in ('python -c "import secrets; print(secrets.token_hex(32))" 2^>nul') do set GENERATED_SK=%%i
if "!GENERATED_SK!"=="" (
    REM Fallback: use PowerShell
    for /f "delims=" %%i in ('powershell -Command "[System.Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLower()"') do set GENERATED_SK=%%i
)
call :set_env SECRET_KEY "!GENERATED_SK!"
echo   ✓  SECRET_KEY auto-generated

:after_secret

REM Prompt for GEMINI_API_KEY if missing
if "!GEMINI_API_KEY!"=="" goto :need_gemini
if "!GEMINI_API_KEY:~0,7!"=="REPLACE" goto :need_gemini
goto :after_gemini

:need_gemini
echo.
echo   ⚠  GEMINI_API_KEY is required.
echo      Get a FREE key at: https://aistudio.google.com/app/apikey
echo.
set /p GEMINI_INPUT="  Paste your Gemini API key: "
if "!GEMINI_INPUT!"=="" (
    echo   ✗  Gemini API key cannot be empty.
    pause
    exit /b 1
)
call :set_env GEMINI_API_KEY "!GEMINI_INPUT!"
echo   ✓  GEMINI_API_KEY saved

:after_gemini

REM Prompt for ADMIN_PASSWORD if missing
if "!ADMIN_PASSWORD!"=="" goto :need_password
if "!ADMIN_PASSWORD:~0,7!"=="REPLACE" goto :need_password
goto :after_password

:need_password
echo.
echo   ⚠  ADMIN_PASSWORD is required for the instructor login.
set /p ADMIN_PW_INPUT="  Choose an admin password (min 8 chars): "
if "!ADMIN_PW_INPUT!"=="" (
    echo   ✗  Admin password cannot be empty.
    pause
    exit /b 1
)
call :set_env ADMIN_PASSWORD "!ADMIN_PW_INPUT!"
echo   ✓  ADMIN_PASSWORD saved

:after_password
echo   ✓  .env is ready

REM ── 3. Create required directories ────────────────────────────────────────
echo   → Creating required directories...
if not exist "data\uploads" mkdir "data\uploads"
if not exist "Book" mkdir "Book"
echo   ✓  data\uploads and Book\ directories ready

REM ── 4. Build Docker images ────────────────────────────────────────────────
echo   → Building Docker images (first run may take 5-10 minutes)...
%DC% build
if errorlevel 1 (
    echo   ✗  Docker build failed. Check output above.
    pause
    exit /b 1
)
echo   ✓  Images built

REM ── 5. Start all services ─────────────────────────────────────────────────
echo   → Starting all services...
%DC% up -d
if errorlevel 1 (
    echo   ✗  Failed to start services.
    pause
    exit /b 1
)
echo   ✓  Containers started

REM ── 6. Wait for backend health ────────────────────────────────────────────
echo   → Waiting for backend to be ready (up to 3 min)...
set TRIES=0
:wait_backend
set /a TRIES+=1
if %TRIES% gtr 90 (
    echo   ✗  Backend did not start in time.
    echo      Check logs: %DC% logs backend
    pause
    exit /b 1
)
curl -sf http://localhost:8000/health >nul 2>&1
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto :wait_backend
)
echo   ✓  Backend is healthy

echo   → Waiting for frontend to be ready...
set TRIES=0
:wait_frontend
set /a TRIES+=1
if %TRIES% gtr 90 (
    echo   ⚠  Frontend is still starting — try http://localhost:3000 in a moment.
    goto :done
)
curl -sf http://localhost:3000 >nul 2>&1
if errorlevel 1 (
    timeout /t 2 /nobreak >nul
    goto :wait_frontend
)
echo   ✓  Frontend is healthy

REM ── 7. Done ───────────────────────────────────────────────────────────────
:done
echo.
echo   ╔═══════════════════════════════════════════════╗
echo   ║   ✓  QuizMark is up and running!              ║
echo   ╚═══════════════════════════════════════════════╝
echo.
echo   App          -^>  http://localhost:3000
echo   API docs     -^>  http://localhost:8000/docs
echo   MongoDB UI   -^>  http://localhost:8081
echo.
echo   Login with:  username=!ADMIN_USERNAME!  and the password you set.
echo.
echo   Next steps:
echo     1. Log in at http://localhost:3000
echo     2. Go to 'Add Book' and upload a PDF textbook
echo     3. Once ingested, open Library -^> click book -^> Generate Questions
echo.
echo   Useful commands:
echo     Stop:        %DC% down
echo     Restart:     %DC% up -d
echo     Logs:        %DC% logs -f
echo     Full reset:  %DC% down -v   (deletes ALL data)
echo.
pause
exit /b 0


REM ── Subroutines ───────────────────────────────────────────────────────────

:read_env
REM Usage: call :read_env VAR_NAME DEFAULT_VALUE
set "%~1=%~2"
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    set "line_key=%%A"
    set "line_key=!line_key: =!"
    if /i "!line_key!"=="%~1" set "%~1=%%B"
)
exit /b 0

:set_env
REM Usage: call :set_env KEY VALUE
REM Replaces or appends a key=value line in .env
set "target_key=%~1"
set "target_val=%~2"
set "found=0"
set "tmpfile=%TEMP%\quizmark_env_tmp.txt"
if exist "!tmpfile!" del "!tmpfile!"
for /f "usebackq delims=" %%L in (".env") do (
    set "thisline=%%L"
    set "linekey=!thisline:*=!"
    for /f "tokens=1 delims==" %%K in ("!thisline!") do (
        if /i "%%K"=="!target_key!" (
            echo !target_key!=!target_val!>> "!tmpfile!"
            set "found=1"
        ) else (
            echo %%L>> "!tmpfile!"
        )
    )
)
if "!found!"=="0" echo !target_key!=!target_val!>> "!tmpfile!"
move /y "!tmpfile!" ".env" >nul
exit /b 0
