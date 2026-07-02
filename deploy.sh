#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#  QuizMark – Production deploy  (see docs/DEPLOYMENT_CLOUD.md)
#  Usage:  bash deploy.sh
#
#  Runs the stack with docker-compose.prod.yml on top of the base file:
#  127.0.0.1-bound ports (put Caddy/Nginx in front), no flower/mongo-express,
#  MongoDB Atlas by default (COMPOSE_PROFILES=local-db to run it on this VM),
#  ENVIRONMENT=production, log rotation.
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")"

GRN='\033[0;32m'; YLW='\033[1;33m'; RED='\033[0;31m'; CYN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e "${GRN}  ✓  $*${NC}"; }
info() { echo -e "${CYN}  →  $*${NC}"; }
warn() { echo -e "${YLW}  ⚠  $*${NC}"; }
die()  { echo -e "${RED}  ✗  $*${NC}"; exit 1; }

DC="docker compose -f docker-compose.yml -f docker-compose.prod.yml"

echo ""
echo -e "${CYN}  ╔══════════════════════════════════════════╗${NC}"
echo -e "${CYN}  ║      QuizMark  —  Production deploy      ║${NC}"
echo -e "${CYN}  ╚══════════════════════════════════════════╝${NC}"
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
command -v docker > /dev/null 2>&1 || die "Docker not found."
docker info > /dev/null 2>&1      || die "Docker daemon is not running."
docker compose version > /dev/null 2>&1 || die "Docker Compose v2 required (v2.24+ for the prod override)."
ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"

# ── 2. Branch sanity ──────────────────────────────────────────────────────────
if command -v git > /dev/null 2>&1 && git rev-parse --git-dir > /dev/null 2>&1; then
  BRANCH=$(git rev-parse --abbrev-ref HEAD)
  if [ "$BRANCH" != "Stable" ]; then
    warn "You are on branch '${BRANCH}' — production should deploy from 'Stable'."
    read -r -p "  Continue anyway? [y/N] " REPLY
    [[ "$REPLY" =~ ^[Yy]$ ]] || die "Aborted. Run: git checkout Stable && git pull --ff-only"
  else
    ok "On Stable branch"
  fi
fi

# ── 3. .env validation ────────────────────────────────────────────────────────
[ -f .env ] || die "No .env file. Copy .env.example and fill in secrets first."
_val() { grep -m1 "^${1}=" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'"; }

ENVIRONMENT=$(_val ENVIRONMENT)
[ "$ENVIRONMENT" = "production" ] || warn "ENVIRONMENT=${ENVIRONMENT:-unset} in .env — the prod override forces 'production' in containers, but set it in .env too."

SK=$(_val SECRET_KEY)
{ [ -n "$SK" ] && [[ "$SK" != REPLACE* ]] && [ "${#SK}" -ge 32 ]; } || die "SECRET_KEY missing/placeholder/too short. Generate: python3 -c 'import secrets; print(secrets.token_hex(32))'"

AP=$(_val ADMIN_PASSWORD)
{ [ -n "$AP" ] && [[ "$AP" != REPLACE* ]] && [ "${#AP}" -ge 8 ]; } || die "ADMIN_PASSWORD missing, placeholder, or under 8 characters."

PU=$(_val PUBLIC_API_URL)
[ -n "$PU" ] || die "PUBLIC_API_URL missing in .env — public https URL of the API, e.g. https://api.quiz.college.edu"
[[ "$PU" == https://* ]] || warn "PUBLIC_API_URL is not https — browsers will block mixed content behind TLS."

CO=$(_val CORS_ORIGINS)
[[ "$CO" == *localhost* ]] && warn "CORS_ORIGINS still contains localhost — set it to your public frontend origin."

MU=$(_val MONGODB_URL)
if [[ "$MU" == *"mongodb://mongodb"* || -z "$MU" ]]; then
  if [[ "${COMPOSE_PROFILES:-}" != *local-db* ]]; then
    die "MONGODB_URL points at the local container but the local DB is disabled in prod.
      Either set MONGODB_URL to your Atlas connection string,
      or run the DB on this VM:  COMPOSE_PROFILES=local-db bash deploy.sh"
  fi
  warn "Running MongoDB on this VM (local-db profile) — remember nightly mongodump backups."
else
  ok "MongoDB: external cluster"
fi

for key in GEMINI_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY; do
  V=$(_val "$key")
  { [ -n "$V" ] && [[ "$V" != REPLACE* ]]; } || die "$key missing or placeholder in .env"
done
ok ".env validated"

# ── 4. Build + start ──────────────────────────────────────────────────────────
info "Building images…"
$DC build --pull
ok "Images built"

info "Starting services…"
$DC up -d --remove-orphans
ok "Services started"

# ── 5. Health check ───────────────────────────────────────────────────────────
info "Waiting for backend…"
MAX=90; I=0
until curl -sf http://127.0.0.1:8000/health > /dev/null 2>&1; do
  I=$((I+1)); [ $I -ge $MAX ] && { echo ""; die "Backend did not become healthy. Run: $DC logs backend"; }
  printf '.'; sleep 2
done
echo ""; ok "Backend healthy"

info "Waiting for frontend…"
MAX=90; I=0
until curl -sf http://127.0.0.1:3000 > /dev/null 2>&1; do
  I=$((I+1)); [ $I -ge $MAX ] && { warn "Frontend still starting — check: $DC logs frontend"; break; }
  printf '.'; sleep 2
done
echo ""; ok "Frontend healthy"

# ── 6. Cleanup ────────────────────────────────────────────────────────────────
docker image prune -f > /dev/null && ok "Dangling images pruned"

echo ""
echo -e "${GRN}  ╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GRN}  ║   ✓  QuizMark production stack is running        ║${NC}"
echo -e "${GRN}  ╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo "  App and API are bound to 127.0.0.1 — point your reverse proxy at:"
echo "    frontend →  127.0.0.1:3000"
echo "    API      →  127.0.0.1:8000   (must be reachable at PUBLIC_API_URL)"
echo ""
echo "  Post-deploy checklist (docs/DEPLOYMENT_CLOUD.md §2):"
echo "    • TLS via Caddy/Nginx or Cloudflare"
echo "    • Firewall: only 80/443 open"
echo "    • Nightly mongodump to object storage"
echo "    • Uptime monitor on /health"
echo ""
