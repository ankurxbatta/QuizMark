@echo off
REM setup.bat – First-run initialisation (Windows)
setlocal

echo =^> Checking for .env...
if not exist .env (
    copy .env.example .env
    echo.
    echo   .env created – edit SECRET_KEY, POSTGRES_PASSWORD, and ADMIN_PASSWORD then run setup.bat again.
    echo.
    exit /b 1
)

echo =^> Pulling Docker images...
docker compose pull --quiet

echo =^> Building images...
docker compose build --quiet

echo =^> Starting database and broker...
docker compose up -d db broker

echo =^> Waiting 15 seconds for Postgres...
timeout /t 15 /nobreak >nul

echo =^> Running Alembic migrations...
docker compose run --rm --no-deps backend alembic upgrade head

echo =^> Starting Ollama...
docker compose up -d llm
timeout /t 8 /nobreak >nul

echo =^> Pulling models (this may take several minutes)...
for /f "tokens=2 delims==" %%A in ('findstr "^LLM_MODEL_NAME"  .env') do set LLM_MODEL=%%A
for /f "tokens=2 delims==" %%A in ('findstr "^SLM_MODEL_NAME"  .env') do set SLM_MODEL=%%A
for /f "tokens=2 delims==" %%A in ('findstr "^EMBEDDING_MODEL" .env') do set EMBED_MODEL=%%A

docker compose exec llm ollama pull %LLM_MODEL%
docker compose exec llm ollama pull %SLM_MODEL%
docker compose exec llm ollama pull %EMBED_MODEL%

echo =^> Starting all services...
docker compose up -d

echo.
echo Setup complete!
echo    Frontend   -^>  http://localhost:3000
echo    API docs   -^>  http://localhost:8000/docs
echo    Tier-1 SLM : %SLM_MODEL%
echo    Tier-3 LLM : %LLM_MODEL%
echo    Embeddings : %EMBED_MODEL%
