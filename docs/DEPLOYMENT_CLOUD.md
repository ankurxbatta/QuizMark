# QuizMark — Secure, Scalable Cloud Deployment Plan (College)

A pragmatic path from this repo to a college-grade deployment. Phase 1 serves a
department (≤ ~2,000 students) on one VM for roughly **$40–70/month + AI usage**;
Phase 2 scales out without re-architecting.

---

## 0. Reality check (what the code assumes today)

- Single-region, single-writer MongoDB; vector search relies on
  **mongodb-atlas-local** locally → use **MongoDB Atlas** (M10+) in the cloud, which
  provides the same `$vectorSearch` natively, plus backups and encryption.
- The rate limiter is **in-process** — correct on ONE backend replica; move to
  Redis before running multiple API replicas.
- Auth is JWT with a 30-minute expiry; exports/SSE pass tokens in query params —
  fine over HTTPS, but keep access logs private.
- AI spend is the real variable cost. Budget per-book ingest (vision) and
  per-question generation/marking; the free Gemini embedding tier is NOT
  college-grade (you exhausted it in one afternoon of testing — get paid quota).

## 1. Architecture (Phase 1 — one VM, all containers)

```
Internet → Cloudflare (DNS + TLS + WAF/rate limit)
        → VM (4 vCPU / 16 GB): Caddy or Nginx reverse proxy
            ├─ frontend  (Next.js, :3000, internal)
            ├─ backend   (FastAPI, :8000, internal)
            ├─ workers   (ingest, gen, mark, clean; merge vision/math/embed/deepsearch
            │             queues into 2 workers at low volume)
            └─ redis     (broker, internal only)
        → MongoDB Atlas M10 (managed, same region, private/IP-allowlisted)
```

Provider suggestions: Hetzner CPX41 / DigitalOcean 16 GB / AWS t3.xlarge —
whatever your college can procure. Region nearest campus.

## 2. Security hardening checklist (do ALL before go-live)

**Network**
- [ ] Only 80/443 open; SSH key-only on a non-default port or behind Tailscale/VPN.
- [ ] Redis and all app ports bound to the Docker network / localhost only
      (the compose file already binds infra ports to 127.0.0.1 — keep that).
- [ ] Flower and mongo-express: DO NOT deploy in production (remove the services
      or gate behind VPN). They are unauthenticated admin surfaces.
- [ ] Atlas: enable IP allowlist (VM's static IP only) + TLS + SCRAM user with
      least-privilege role on the one database.

**Application**
- [ ] `ENVIRONMENT=production` (disables /docs and demo seeding).
- [ ] Fresh `SECRET_KEY` (64 hex), strong `ADMIN_PASSWORD`, rotate both per term.
- [ ] `CORS_ORIGINS` = exactly your public frontend origin (https://quiz.college.edu).
- [ ] JWT expiry: keep 30 min; consider 8h only for instructor role if sessions annoy.
- [ ] Create per-student accounts via /auth/register batch script; never share logins.
- [ ] Backups: nightly `mongodump` to object storage (S3/B2) with 30-day retention
      — Atlas M10 also has continuous backup; enable it.
- [ ] Keep API keys ONLY in the VM's .env (never in git — already enforced).

**Operations**
- [ ] `docker compose pull/build` via a deploy script from a tagged release branch
      (Stable), never from Develop.
- [ ] Log rotation (docker `--log-opt max-size=50m`), plus Uptime monitoring
      (UptimeRobot on /health) and disk alerts at 80%.
- [ ] OS auto security updates (unattended-upgrades / dnf-automatic).

## 3. AI provider setup (the part that actually breaks first)

| Capability | Provider | Action for college scale |
|---|---|---|
| Embeddings | Gemini free tier | **Upgrade to paid** (or switch primary to OpenAI embeddings with credit). 1,000 free requests/day died in one afternoon of testing. |
| Generation/marking | OpenAI + Anthropic | Fund BOTH (fallback is load-bearing — your OpenAI account hit $0 and Anthropic carried generation). Set billing alerts at 50/80%. |
| Vision (ingest) | OpenAI | Budget ≈ per-book one-time cost; vision results are cached by content hash, so re-ingests are nearly free. |
| Web search (DeepSearch) | OpenAI web_search | Works with the same OpenAI key; optional Tavily key as backup. |

Cost controls already in the code: `GEN_MAX_TOTAL_QUESTIONS`, `ASSET_MAX_PER_CHAPTER`
image budget, top-up round caps, bank dedup, quality-gate-aware generation, and
the DeepSearch per-request toggle.

## 4. Scaling path (Phase 2 — when one VM isn't enough)

Trigger: sustained CPU > 70%, marking queue latency > 2 min, or > ~2k active students.

1. **Split workers from web**: second VM runs only workers (same compose file,
   different service selection). Redis stays with the API VM or moves to managed
   Redis. No code change — Celery is already queue-per-capability.
2. **Redis-backed rate limiter** (the TODO in `core/security.py`) before running
   2+ backend replicas behind the proxy.
3. **Atlas auto-scale** M10 → M20/M30; vector search stays identical.
4. **Object storage for PDFs**: GridFS is fine to ~50 GB; past that, move
   `book_pdfs` to S3-compatible storage (isolated in `mongo_vector_store` — one
   module to change).
5. **Multi-tenancy**: if other departments join, scope exports and books by
   course/cohort (flagged in the security review — currently every instructor
   sees all data by design).

## 5. Rollout plan

| Week | Milestone |
|---|---|
| 1 | Provision VM + Atlas; deploy Stable; ingest 1 course book; smoke-test with 3 pilot students |
| 2 | Pilot course (1 instructor, ~30 students): 1 quiz cycle incl. marking + overrides + export |
| 3 | Fix pilot findings; load-test submissions (the unique-index race is covered by tests); enable backups + monitoring alerts |
| 4 | Department go-live; weekly mongodump restore drill once; document the admin runbook |

**Deploy commands** (VM):
```bash
git clone -b Stable <repo> && cd marking-tools
cp .env.example .env   # fill secrets + Atlas URI + AI keys
# remove flower + mongo-express from docker-compose.yml (or create a prod override)
docker compose up -d --build
```

## 6. What I'd fix in code before go-live (small, known)

1. Redis rate limiter (only if >1 backend replica).
2. Session UX: 30-min JWT + long ingests means instructors re-login mid-job;
   jobs survive (server-side), but consider a refresh-token endpoint.
3. Export scoping per course if instructors must not see each other's cohorts.
