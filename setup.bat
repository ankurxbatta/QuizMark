@echo off
REM setup.bat - First-run initialization (Windows)
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo =^> Checking Docker Desktop...
docker version >nul 2>&1
if errorlevel 1 (
    echo Docker Desktop does not look reachable. Start Docker Desktop and try again.
    exit /b 1
)

echo =^> Checking for .env...
if not exist .env (
    copy .env.example .env
    echo.
    echo   .env created - edit SECRET_KEY, POSTGRES_PASSWORD, and ADMIN_PASSWORD then run setup.bat again.
    echo.
    exit /b 1
)

call :read_env POSTGRES_USER quizuser
call :read_env POSTGRES_DB quizdb
call :read_env LLM_MODEL_NAME qwen2:1.5b
call :read_env SLM_MODEL_NAME qwen2:0.5b
call :read_env EMBEDDING_MODEL nomic-embed-text

echo =^> Pulling Docker images for third-party services...
docker compose pull --quiet
if errorlevel 1 goto :fail

echo =^> Building images...
docker compose build
if errorlevel 1 goto :fail

echo =^> Starting database and broker...
docker compose up -d db broker
if errorlevel 1 goto :fail

echo =^> Waiting for Postgres...
for /l %%I in (1,1,30) do (
    docker compose exec -T db pg_isready -U "%POSTGRES_USER%" -d "%POSTGRES_DB%" >nul 2>&1
    if not errorlevel 1 goto :postgres_ready
    timeout /t 2 /nobreak >nul
)
echo Postgres did not become ready in time.
docker compose logs --tail=80 db
goto :fail

:postgres_ready
echo Postgres is ready.

echo =^> Running Alembic migrations...
docker compose run --rm --no-deps backend alembic upgrade head
if errorlevel 1 goto :fail

echo =^> Starting Ollama...
docker compose up -d llm
if errorlevel 1 goto :fail

echo =^> Waiting for Ollama API...
for /l %%I in (1,1,60) do (
    docker compose exec -T llm ollama list >nul 2>&1
    if not errorlevel 1 goto :ollama_ready
    timeout /t 3 /nobreak >nul
)
echo Ollama did not respond in time.
docker compose logs --tail=80 llm
goto :fail

:ollama_ready
echo =^> Pulling models (this may take several minutes)...
docker compose exec -T llm ollama pull "%LLM_MODEL_NAME%"
if errorlevel 1 goto :fail
docker compose exec -T llm ollama pull "%SLM_MODEL_NAME%"
if errorlevel 1 goto :fail
docker compose exec -T llm ollama pull "%EMBEDDING_MODEL%"
if errorlevel 1 goto :fail

echo =^> Starting all services...
docker compose up -d
if errorlevel 1 goto :fail

echo.
echo Setup complete!
echo    Frontend   -^>  http://localhost:3000
echo    API docs   -^>  http://localhost:8000/docs
echo    Ollama     -^>  http://localhost:11434
echo    Tier-1 SLM : %SLM_MODEL_NAME%
echo    Tier-3 LLM : %LLM_MODEL_NAME%
echo    Embeddings : %EMBEDDING_MODEL%
exit /b 0

:read_env
set "%~1=%~2"
for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /I "%%A"=="%~1" set "%~1=%%B"
)
exit /b 0

:fail
echo.
echo Setup failed. Check the Docker output above.
echo Useful diagnostics:
echo   docker compose ps -a
echo   docker compose logs llm
echo   docker compose logs backend
exit /b 1
