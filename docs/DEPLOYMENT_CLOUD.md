# QuizMark — Secure, Scalable Cloud Deployment Plan (College, AWS)

A pragmatic path from this repo to a college-grade deployment **on AWS** (decision
2026-07-02). Phase 1 serves a department (≤ ~2,000 students) on one EC2 instance
for roughly **$75–130/month + Atlas M10 (~$60) + AI usage**; Phase 2 scales out
without re-architecting.

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

## 1. Architecture (Phase 1 — one EC2 instance, all containers)

```
Internet → Cloudflare (DNS + TLS + WAF/rate limit)   [or Route 53 + ACM + ALB]
        → EC2 t3.xlarge (4 vCPU / 16 GB, Elastic IP): Caddy or Nginx reverse proxy
            ├─ frontend  (Next.js, :3000, 127.0.0.1 only)
            ├─ backend   (FastAPI, :8000, 127.0.0.1 only)
            ├─ workers   (ingest, gen, mark, clean; merge vision/math/embed/deepsearch
            │             queues into 2 workers at low volume)
            └─ redis     (broker, docker network only)
        → MongoDB Atlas M10 (deployed ON AWS, same region, IP-allowlisted)
```

**AWS specifics**
- **Instance**: `t3.xlarge` (4 vCPU / 16 GB) with a 1-year Compute Savings Plan
  (~$75/mo vs ~$121 on-demand). Starting smaller (`t3.large`, 8 GB) works for a
  pilot but ingestion + 8 workers will swap; upgrade before go-live.
- **Storage**: 100 GB gp3 EBS, encrypted, `DeleteOnTermination=false`.
- **Networking**: Elastic IP; security group allows inbound **80/443 only**
  (SSH via AWS SSM Session Manager instead of an open port 22 — free and audited).
- **MongoDB**: Atlas M10 with AWS as the cloud provider, same region as the EC2
  instance; allowlist the Elastic IP only. (Do NOT use DocumentDB — it has no
  `$vectorSearch`.)
- **Backups**: nightly `mongodump` to an S3 bucket with a 30-day lifecycle rule
  (plus Atlas continuous backup); EBS snapshot weekly via Data Lifecycle Manager.
- **Cost guardrail**: AWS Budgets alert at your monthly cap; the college's AWS
  Educate / academic credits often cover a pilot term.

## 2. Security hardening checklist (do ALL before go-live)

**Network**
- [ ] Security group: inbound 80/443 only; shell access via SSM Session Manager
      (no open port 22).
- [ ] Redis and all app ports bound to the Docker network / localhost only —
      `docker-compose.prod.yml` already does this (frontend/backend on 127.0.0.1).
- [ ] Flower and mongo-express never start in production —
      `docker-compose.prod.yml` disables them via a never-activated profile.
- [ ] Atlas: enable IP allowlist (the Elastic IP only) + TLS + SCRAM user with
      least-privilege role on the one database.

**Application**
- [ ] `ENVIRONMENT=production` (disables /docs and demo seeding).
- [ ] Fresh `SECRET_KEY` (64 hex), strong `ADMIN_PASSWORD`, rotate both per term.
- [ ] `CORS_ORIGINS` = exactly your public frontend origin (https://quiz.college.edu).
- [ ] JWT expiry: keep 30 min; consider 8h only for instructor role if sessions annoy.
- [ ] Create per-student accounts via /auth/register batch script; never share logins.
- [ ] Backups: nightly `mongodump` to S3 with a 30-day lifecycle rule
      — Atlas M10 also has continuous backup; enable it.
- [ ] Keep API keys ONLY in the instance's .env (never in git — already enforced).

**Operations**
- [ ] Deploy with `bash deploy.sh` from the `Stable` branch, never from Develop —
      the script validates .env (secret strength, PUBLIC_API_URL, Atlas URL),
      warns off-branch, builds, starts, and health-checks.
- [ ] Log rotation is set by `docker-compose.prod.yml` (50 MB × 3 per container);
      add CloudWatch agent or UptimeRobot on /health and disk alerts at 80%.
- [ ] OS auto security updates (`dnf-automatic` on Amazon Linux 2023 /
      `unattended-upgrades` on Ubuntu).

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

**Deploy commands** (EC2 instance):
```bash
git clone -b Stable <repo> && cd marking-tools
cp .env.example .env   # fill secrets + Atlas URI + AI keys + PUBLIC_API_URL
bash deploy.sh         # validates .env, builds, starts docker-compose.prod.yml, health-checks
```
`deploy.sh` refuses placeholder secrets and a local-container `MONGODB_URL`
(unless you opt into `COMPOSE_PROFILES=local-db`), and never starts flower or
mongo-express. Point the reverse proxy at 127.0.0.1:3000 (app) and
127.0.0.1:8000 (API, must be reachable at `PUBLIC_API_URL`).

## 6. What I'd fix in code before go-live (small, known)

1. Redis rate limiter (only if >1 backend replica).
2. ~~Session UX: 30-min JWT + long ingests means instructors re-login mid-job~~
   **Done 2026-07-02**: `POST /auth/refresh` + frontend silent refresh; sessions
   slide up to `SESSION_MAX_MINUTES` (12 h default), then force re-login.
3. Export scoping per course if instructors must not see each other's cohorts.
