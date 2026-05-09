@echo off
REM setup.bat – First-run initialisation for Windows

echo =^> Copying environment template...
if not exist .env (
    copy .env.example .env
    echo     .env created – please review and set your secrets before continuing.
    exit /b 1
)

echo =^> Pulling Docker images...
docker compose pull

echo =^> Building application images...
docker compose build

echo =^> Starting db and broker...
docker compose up -d db broker

echo =^> Waiting for database (10 seconds)...
timeout /t 10 /nobreak >nul

echo =^> Running Alembic migrations...
docker compose run --rm backend alembic upgrade head

echo =^> Pulling LLM models...
for /f "tokens=2 delims==" %%A in ('findstr "LLM_MODEL_NAME" .env') do set LLM_MODEL=%%A
for /f "tokens=2 delims==" %%A in ('findstr "EMBEDDING_MODEL" .env') do set EMBED_MODEL=%%A
docker compose run --rm llm ollama pull %LLM_MODEL%
docker compose run --rm llm ollama pull %EMBED_MODEL%

echo =^> Starting all services...
docker compose up -d

echo.
echo Setup complete!
echo    Frontend : http://localhost:3000
echo    Backend  : http://localhost:8000/docs
