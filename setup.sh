#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#  QuizMark – First-run setup  (Linux / macOS)
#  Usage:  bash setup.sh
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
GRN='\033[0;32m'; YLW='\033[1;33m'; RED='\033[0;31m'; CYN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GRN}  ✓  $*${NC}"; }
info() { echo -e "${CYN}  →  $*${NC}"; }
warn() { echo -e "${YLW}  ⚠  $*${NC}"; }
die()  { echo -e "${RED}  ✗  $*${NC}"; exit 1; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${CYN}  ╔═══════════════════════════════════════╗${NC}"
echo -e "${CYN}  ║         QuizMark  –  Setup            ║${NC}"
echo -e "${CYN}  ╚═══════════════════════════════════════╝${NC}"
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
info "Checking prerequisites…"

command -v docker > /dev/null 2>&1 \
  || die "Docker not found. Install Docker Desktop: https://docs.docker.com/get-docker/"

docker info > /dev/null 2>&1 \
  || die "Docker is not running. Start Docker Desktop and try again."

# Accept both 'docker compose' (v2) and 'docker-compose' (v1)
if docker compose version > /dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose > /dev/null 2>&1; then
  DC="docker-compose"
else
  die "Docker Compose not found. Update Docker Desktop or install docker-compose."
fi

ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"
ok "Docker Compose ($DC)"

# ── 2. .env setup ─────────────────────────────────────────────────────────────
info "Checking .env…"

if [ ! -f .env ]; then
  [ -f .env.example ] || die ".env.example not found. Re-clone the repository."
  cp .env.example .env
  ok ".env created from .env.example"
fi

# Helper: read a value from .env
_val() { grep -m1 "^${1}=" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'"; }

# Helper: set/replace a value in .env (portable sed)
_set() {
  local key=$1 val=$2
  if grep -q "^${key}=" .env; then
    sed -i.bak "s|^${key}=.*|${key}=${val}|" .env && rm -f .env.bak
  else
    echo "${key}=${val}" >> .env
  fi
}

# Auto-generate SECRET_KEY if still placeholder
SK=$(_val SECRET_KEY)
if [ -z "$SK" ] || [[ "$SK" == REPLACE* ]]; then
  if command -v python3 > /dev/null 2>&1; then
    SK=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  elif command -v openssl > /dev/null 2>&1; then
    SK=$(openssl rand -hex 32)
  else
    SK=$(cat /dev/urandom | LC_ALL=C tr -dc 'a-f0-9' | head -c 64)
  fi
  _set SECRET_KEY "$SK"
  ok "SECRET_KEY auto-generated"
fi

# Prompt for GEMINI_API_KEY if missing
GK=$(_val GEMINI_API_KEY)
if [ -z "$GK" ] || [[ "$GK" == REPLACE* ]]; then
  echo ""
  warn "GEMINI_API_KEY is required (used for embeddings, generation, and chart vision)."
  echo -e "  Get a FREE key at: ${CYN}https://aistudio.google.com/app/apikey${NC}"
  echo ""
  read -r -p "  Paste your Gemini API key: " GK
  [ -z "$GK" ] && die "Gemini API key cannot be empty."
  _set GEMINI_API_KEY "$GK"
  ok "GEMINI_API_KEY saved"
fi

# Prompt for ADMIN_PASSWORD if still placeholder
AP=$(_val ADMIN_PASSWORD)
if [ -z "$AP" ] || [[ "$AP" == REPLACE* ]]; then
  echo ""
  warn "ADMIN_PASSWORD is required for the instructor login."
  while true; do
    read -r -s -p "  Choose an admin password (min 8 chars): " AP; echo ""
    [ "${#AP}" -ge 8 ] && break
    warn "Password must be at least 8 characters."
  done
  _set ADMIN_PASSWORD "$AP"
  ok "ADMIN_PASSWORD saved"
fi

ok ".env is ready"

# ── 3. Create required directories ────────────────────────────────────────────
info "Creating required directories…"
mkdir -p data/uploads Book
ok "data/uploads and Book/ directories ready"

# ── 4. Build Docker images ────────────────────────────────────────────────────
info "Building Docker images (first run may take 5–10 minutes)…"
$DC build
ok "Images built"

# ── 5. Start all services ─────────────────────────────────────────────────────
info "Starting all services…"
$DC up -d
ok "Containers started"

# ── 6. Wait for health ────────────────────────────────────────────────────────
info "Waiting for backend to be ready…"
MAX=90; I=0
until curl -sf http://localhost:8000/health > /dev/null 2>&1; do
  I=$((I+1))
  [ $I -ge $MAX ] && {
    echo ""
    die "Backend did not start in time. Check logs: $DC logs backend"
  }
  printf '.'
  sleep 2
done
echo ""
ok "Backend is healthy"

info "Waiting for frontend to be ready…"
MAX=90; I=0
until curl -sf http://localhost:3000 > /dev/null 2>&1; do
  I=$((I+1))
  [ $I -ge $MAX ] && { warn "Frontend is still starting — try http://localhost:3000 in a moment."; break; }
  printf '.'
  sleep 2
done
echo ""
ok "Frontend is healthy"

# ── 7. Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GRN}  ╔═══════════════════════════════════════════════╗${NC}"
echo -e "${GRN}  ║   ✓  QuizMark is up and running!              ║${NC}"
echo -e "${GRN}  ╚═══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYN}App${NC}          →  http://localhost:3000"
echo -e "  ${CYN}API docs${NC}     →  http://localhost:8000/docs"
echo -e "  ${CYN}MongoDB UI${NC}   →  http://localhost:8081"
echo ""
ADMIN_USER=$(_val ADMIN_USERNAME)
echo -e "  Login with:  ${YLW}username=${ADMIN_USER:-admin}${NC}  and the password you set."
echo ""
echo "  Next steps:"
echo "    1. Log in at http://localhost:3000"
echo "    2. Go to 'Add Book' and upload a PDF textbook"
echo "    3. Once ingested, open Library → click the book → Generate Questions"
echo ""
echo "  Useful commands:"
echo "    Stop:        $DC down"
echo "    Restart:     $DC up -d"
echo "    Logs:        $DC logs -f"
echo "    Full reset:  $DC down -v   (deletes ALL data)"
echo ""
