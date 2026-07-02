#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#  QuizMark – First-run setup  (Mac / Linux)
#  Usage:  bash setup.sh
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

GRN='\033[0;32m'; YLW='\033[1;33m'; RED='\033[0;31m'; CYN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GRN}  ✓  $*${NC}"; }
info() { echo -e "${CYN}  →  $*${NC}"; }
warn() { echo -e "${YLW}  ⚠  $*${NC}"; }
die()  { echo -e "${RED}  ✗  $*${NC}"; exit 1; }

echo ""
echo -e "${CYN}  ╔══════════════════════════════════════════╗${NC}"
echo -e "${CYN}  ║          QuizMark  —  Setup              ║${NC}"
echo -e "${CYN}  ╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
info "Checking prerequisites…"
command -v docker > /dev/null 2>&1 || die "Docker not found. Install Docker Desktop: https://docs.docker.com/get-docker/"
docker info > /dev/null 2>&1      || die "Docker is not running. Start Docker Desktop and try again."

if docker compose version > /dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose > /dev/null 2>&1; then
  DC="docker-compose"
else
  die "Docker Compose not found. Update Docker Desktop or install docker-compose."
fi
ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
ok "Compose ($DC)"

# ── 2. .env ───────────────────────────────────────────────────────────────────
info "Checking .env…"
[ -f .env ] || { [ -f .env.example ] || die ".env.example not found. Re-clone the repo."; cp .env.example .env; ok ".env created"; }

_val() { grep -m1 "^${1}=" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'"; }
_set() {
  local k=$1 v=$2
  # escape sed-special chars (\ & and the | delimiter) so passwords
  # containing them don't corrupt the .env line
  local esc
  esc=$(printf '%s' "$v" | sed -e 's/[\\&|]/\\&/g')
  if grep -q "^${k}=" .env; then
    sed -i.bak "s|^${k}=.*|${k}=${esc}|" .env && rm -f .env.bak
  else
    echo "${k}=${v}" >> .env
  fi
}

# SECRET_KEY
SK=$(_val SECRET_KEY)
if [ -z "$SK" ] || [[ "$SK" == REPLACE* ]]; then
  SK=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null \
       || openssl rand -hex 32 2>/dev/null \
       || cat /dev/urandom | LC_ALL=C tr -dc 'a-f0-9' | head -c 64)
  _set SECRET_KEY "$SK"; ok "SECRET_KEY generated"
fi

# ADMIN_PASSWORD
AP=$(_val ADMIN_PASSWORD)
if [ -z "$AP" ] || [[ "$AP" == REPLACE* ]]; then
  echo ""
  warn "Choose an admin password for the instructor login."
  while true; do
    read -r -s -p "  Admin password (min 8 chars): " AP; echo ""
    [ "${#AP}" -ge 8 ] && break
    warn "Must be at least 8 characters."
  done
  _set ADMIN_PASSWORD "$AP"; ok "ADMIN_PASSWORD saved"
fi

# GEMINI_API_KEY
GK=$(_val GEMINI_API_KEY)
if [ -z "$GK" ] || [[ "$GK" == REPLACE* ]]; then
  echo ""
  warn "GEMINI_API_KEY — used for vector embeddings (free tier)."
  echo -e "  Get a free key: ${CYN}https://aistudio.google.com/app/apikey${NC}"
  read -r -p "  Paste Gemini API key: " GK
  [ -z "$GK" ] && die "Gemini API key cannot be empty."
  _set GEMINI_API_KEY "$GK"; ok "GEMINI_API_KEY saved"
fi

# OPENAI_API_KEY
OK=$(_val OPENAI_API_KEY)
if [ -z "$OK" ] || [[ "$OK" == REPLACE* ]]; then
  echo ""
  warn "OPENAI_API_KEY — primary provider for vision, math, generation and marking."
  echo -e "  Get a key: ${CYN}https://platform.openai.com/api-keys${NC}"
  read -r -p "  Paste OpenAI API key: " OK
  [ -z "$OK" ] && die "OpenAI API key cannot be empty."
  _set OPENAI_API_KEY "$OK"; ok "OPENAI_API_KEY saved"
fi

# ANTHROPIC_API_KEY
AK=$(_val ANTHROPIC_API_KEY)
if [ -z "$AK" ] || [[ "$AK" == REPLACE* ]]; then
  echo ""
  warn "ANTHROPIC_API_KEY — fallback provider (activates when OpenAI hits quota)."
  echo -e "  Get a key: ${CYN}https://console.anthropic.com${NC}"
  read -r -p "  Paste Anthropic API key: " AK
  [ -z "$AK" ] && die "Anthropic API key cannot be empty."
  _set ANTHROPIC_API_KEY "$AK"; ok "ANTHROPIC_API_KEY saved"
fi

ok ".env is ready"

# ── 3. Directories ────────────────────────────────────────────────────────────
info "Creating required directories…"
mkdir -p data/uploads Book
ok "data/uploads and Book/ ready"

# ── 4. Build ──────────────────────────────────────────────────────────────────
info "Building Docker images (first run takes 5–10 minutes)…"
$DC build
ok "Images built"

# ── 5. Start ──────────────────────────────────────────────────────────────────
info "Starting all services…"
$DC up -d
ok "All containers started"

# ── 6. Health checks ──────────────────────────────────────────────────────────
info "Waiting for backend…"
MAX=90; I=0
until curl -sf http://localhost:8000/health > /dev/null 2>&1; do
  I=$((I+1)); [ $I -ge $MAX ] && { echo ""; die "Backend did not start. Run: $DC logs backend"; }
  printf '.'; sleep 2
done
echo ""; ok "Backend healthy"

info "Waiting for frontend…"
MAX=90; I=0
until curl -sf http://localhost:3000 > /dev/null 2>&1; do
  I=$((I+1)); [ $I -ge $MAX ] && { warn "Frontend still starting — try http://localhost:3000 in a moment."; break; }
  printf '.'; sleep 2
done
echo ""; ok "Frontend healthy"

# ── 7. Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GRN}  ╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GRN}  ║   ✓  QuizMark is up and running!                 ║${NC}"
echo -e "${GRN}  ╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYN}App${NC}            →  http://localhost:3000"
echo -e "  ${CYN}API docs${NC}       →  http://localhost:8000/docs"
echo -e "  ${CYN}MongoDB UI${NC}     →  http://localhost:8081"
echo -e "  ${CYN}Worker monitor${NC} →  http://localhost:5555  (Flower)"
echo ""
ADMIN_USER=$(_val ADMIN_USERNAME)
echo -e "  Login:  ${YLW}username=${ADMIN_USER:-admin}${NC}  + the password you set above"
echo ""
echo "  Quick commands:"
echo "    Stop all:        $DC down"
echo "    Restart:         $DC up -d"
echo "    View logs:       $DC logs -f"
echo "    Rebuild:         $DC up -d --build"
echo "    Full reset:      $DC down -v   ← deletes ALL data"
echo ""
echo "  Getting started:"
echo "    1. Log in at http://localhost:3000"
echo "    2. Add Book → upload a PDF textbook (up to 25 MB, 700 pages)"
echo "    3. Wait for ingestion → Library → click book → Generate Questions"
echo ""
