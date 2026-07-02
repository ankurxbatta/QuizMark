# Deployment Options & Cost Analysis

> **Decision (2026-07-02): deploying on AWS** — see `DEPLOYMENT_CLOUD.md` for the
> concrete plan (EC2 + Atlas-on-AWS + S3 backups, `deploy.sh` +
> `docker-compose.prod.yml`). This document is the provider research that
> preceded the decision, kept for reference.

> Research compiled 2026-06-16. All prices are current 2026 figures gathered from vendor pricing pages and reputable aggregators (sources linked at the bottom of each section). **Verify live prices before committing** — several providers changed pricing in mid-2026 (notably Hetzner on 15 Jun 2026 and Oracle's free tier on 15 Jun 2026).

---

## 1. What we're deploying

This is **not** a single app — it's an 11-container docker-compose stack built around bursty, LLM-heavy RAG pipelines.

| Container | Role | Notes |
|---|---|---|
| `frontend` | Next.js 15 (Node) | HTTP, request-driven |
| `backend` | FastAPI + Uvicorn (Python) | HTTP API |
| `worker-ingest` | Celery worker | PDF parse/chunk/orchestrate |
| `worker-vision` | Celery worker | Chart/image description via LLM, `mem_limit: 1g` |
| `worker-math` | Celery worker | Formula extraction via LLM |
| `worker-clean` | Celery worker | CPU-only text cleanup, concurrency=4 |
| `worker-embed` | Celery worker | Embedding generation |
| `worker-deepsearch` | Celery worker | Multi-query RAG retrieval |
| `worker-gen` | Celery worker | Question generation via LLM |
| `worker-mark` | Celery worker | Answer marking via LLM, concurrency=4 |
| `flower` | Celery dashboard | Long-lived monitoring UI |
| `broker` | **Redis** | Celery broker + result backend |
| `mongodb` | **MongoDB Atlas Local** | Primary store **+ Atlas Vector Search** for RAG |
| `mongo-express` | Mongo admin UI | Dev-only, drop in prod |

### Three constraints that drive the whole decision

1. **MongoDB needs Atlas Vector Search.** The stack uses the `mongodb/mongodb-atlas-local` image specifically for vector search (the RAG vector store). This means you either **self-host the Atlas Local container** (free, but you own backups/HA/RAM) or use **managed MongoDB Atlas** (vector search is included free on every tier, even M0). No PaaS/K8s platform offers managed Mongo-with-vector-search natively — it is **always an external add-on**.

2. **Celery workers are long-running pollers, not HTTP services.** They `BRPOP` from Redis several times a second. This means:
   - **Scale-to-zero is useless** for them — every platform's "sleep on HTTP inactivity" never triggers, so you pay for ~10 always-on containers everywhere.
   - **Cloud Run is an architectural mismatch** for the worker fleet (request-driven model; CPU throttled off-request). Fine for frontend + backend only.
   - Per-resource billing (Fly, Railway, Fargate, GKE Autopilot) beats flat per-service tiers (Render, DO App Platform) for this shape.

3. **LLM API cost is separate from infrastructure and usually dwarfs it.** Anthropic + OpenAI + Gemini are pay-per-token. At realistic volumes this is **$26–$340/mo** depending on model routing — often more than the compute. See §6.

### RAM footprint (single box)

| Component | RAM |
|---|---|
| 8 Celery workers (avg ~512 MB, some up to 1 GB) | ~4–6 GB |
| MongoDB (+ `mongot` vector process: +1–2 GB) | 1–2 GB (+1–2 GB) |
| Redis | ~256 MB |
| Next.js | ~512 MB |
| FastAPI | ~512 MB |
| Flower | ~150 MB |
| OS + Docker overhead | ~1 GB |
| **Total** | **~8–11 GB** |

**8 GB is the bare floor (no headroom); 16 GB is the realistic production target**, especially with self-hosted vector search on the same box.

---

## 2. The four deployment strategies

| Strategy | What you do | Best when |
|---|---|---|
| **A. Single VM (IaaS)** | `docker compose up` on one rented Linux box | Cheapest; you accept manual ops + single point of failure |
| **B. PaaS (push-to-deploy)** | Each service becomes a managed container; vendor handles runtime | Want zero server admin; willing to pay 3–6× the VM price |
| **C. Container orchestration / K8s** | ECS/Fargate, GKE, DOKS, etc. with autoscaling | Need real autoscaling on queue depth (KEDA), HA, scale |
| **D. Hybrid (recommended at scale)** | Managed Atlas + managed Redis + app on B or C | Decouple stateful deps from compute for reliability |

---

## 3. Strategy A — Single VM (IaaS, run docker-compose yourself)

Cheapest by a wide margin. You manage the OS, Docker, TLS, backups, and scale vertically (resize + reboot).

### Production box (16 GB, ~100 GB disk, daily backup, ~1 TB egress)

| Provider | Spec | Compute | Storage | Backup | Egress | **Monthly** |
|---|---|---|---|---|---|---|
| **Oracle Cloud Free** | 2 OCPU / 12 GB ARM | $0 | $0 (200 GB incl) | $0 | $0 (10 TB incl) | **~$0** ⚠️ |
| **Hetzner** | CAX31 ARM, 8 vCPU/16 GB | ~$25 | ~$5.70 | ~$5 | ~$0 (20 TB incl) | **~$36** |
| **Linode/Akamai** | Dedicated 16 GB | ~$120 | $10 | ~$30 | ~$0 (incl) | **~$160** |
| **DigitalOcean** | General Purpose 16 GB | $126 | $10 | ~$38 | ~$0 (4 TB incl) | **~$174** |
| **AWS EC2** | m6i.xlarge 4 vCPU/16 GB (1-yr RI) | ~$88 | $8 | ~$5 | ~$90 | **~$191** |
| **AWS EC2** | m6i.xlarge on-demand | ~$140 | $8 | ~$5 | ~$90 | **~$243** |

### Dev box (4 GB)

| Provider | Spec | **Monthly** |
|---|---|---|
| **Oracle Cloud Free** | 2 OCPU / 12 GB ARM | **~$0** |
| **Hetzner** | CX23, 2 vCPU/4 GB | **~$8** |
| **DigitalOcean** | Basic 4 GB | **~$34** |
| **Linode** | Shared 4 GB | **~$35** |
| **AWS EC2** | t3.medium | **~$37** (+ egress) |

**Notes & gotchas:**
- **Hetzner** is the clear value winner for real money. EUR ex-VAT; CPX/CCX lines jumped up to +176% on 15 Jun 2026 — prefer the **CAX (ARM)** or **CX** lines, which rose far less. Verify live prices.
- **Oracle Free** can genuinely run this at $0, but the free ARM allowance was **cut from 24 GB to 12 GB on 15 Jun 2026**, capacity ("Out of Capacity") errors are common, and it's not production-grade. Great for dev/hobby, risky as sole prod host.
- **AWS egress at $0.09/GB is the hidden killer** — 1 TB of traffic ≈ +$90/mo, which can double the bill vs Hetzner/DO. Only pick AWS for ecosystem integration.
- **Self-hosting MongoDB vector search** on the VM is free (Community 8.2+ ships `mongot`), but it's memory-hungry (push to 16 GB), and a single VM means no failover — node loss = downtime.

Sources: [Hetzner pricing](https://www.hetzner.com/cloud) · [Hetzner Jun-2026 increase](https://wz-it.com/en/blog/hetzner-price-increase-june-2026-cpx-ccx-alternatives/) · [DO Droplets](https://www.digitalocean.com/pricing/droplets) · [AWS EC2 on-demand](https://aws.amazon.com/ec2/pricing/on-demand/) · [AWS EBS](https://aws.amazon.com/ebs/pricing/) · [Oracle Free Tier](https://cloudpricecheck.com/free-tier/oracle) · [Linode pricing](https://www.linode.com/pricing/)

---

## 4. Strategy B — PaaS (push-to-deploy)

You give up the single-box cost advantage (you pay per service ×11) in exchange for zero server admin. **Remember: MongoDB+vector search is external on all four** (Atlas M0 free for dev, ~$57+ M10 for prod). Redis is available on-platform.

| Platform | Billing model | Workers scale-to-zero? | Dev/low | Small prod |
|---|---|---|---|---|
| **Fly.io** | Per-resource, per-second; tiny machines from $1.94/mo | No (no HTTP traffic to trigger sleep) | **~$35–50** | **~$100–130** |
| **Railway** | Pure usage-based: $20/vCPU-mo + $10/GB-mo, per-second | No (rewards idle CPU though) | **~$45–60** | **~$140–170** |
| **DigitalOcean App Platform** | Flat per-container tiers ($5/512MB up) | No | **~$75** | **~$200–260** |
| **Render** | Flat tiers ($7 Starter/512MB, $25 Standard/2GB) | No (free tier sleeps on HTTP only — useless for pollers) | **~$90–130** | **~$200–290** |

**Key takeaways:**
- **Fly.io wins on raw cost** — fine-grained machine sizing ($1.94–$15.55) fits 8 small workers cheaply. Managed Redis is via the Upstash extension.
- **Railway wins if workers are CPU-idle between bursts** (per-second CPU billing) and has the best DX + native managed Redis & MongoDB.
- The **1 GB memory-heavy `worker-vision`** breaks the cheapest tier on Render ($7→$25) and DO ($5→$12+), but only costs ~$7.78 on Fly / ~$10 on Railway.
- **DO App Platform has a weak persistent-volume story** — your file-upload volume would need DO Spaces (object storage, ~$5/mo) or a separate Droplet. A real limitation given the stateful `./data/uploads` mount.
- **Render Key Value (Redis)** from $10/mo; **Railway Redis** ~$2.50–$5/mo; **DO Valkey** from $15/mo.

Sources: [Render pricing](https://render.com/pricing) · [Railway pricing](https://railway.com/pricing) · [Fly.io pricing](https://fly.io/docs/about/pricing/) · [DO App Platform pricing](https://docs.digitalocean.com/products/app-platform/details/pricing/)

---

## 5. Strategy C — Container orchestration / Kubernetes

Right when you need **autoscaling on Celery queue depth** (KEDA) and HA. All totals **exclude** external Atlas + Redis.

| Platform | Billing model | Celery fit | Dev | Small prod (11 warm) |
|---|---|---|---|---|
| **DOKS** (DO Kubernetes) | Free control plane (+$40 HA) + Droplet nodes | Excellent (KEDA, Helm) | **~$36** | **~$84–136** |
| **GKE Autopilot** | $0.10/hr cluster (1 free) + per-pod vCPU/GiB | Excellent — best autoscaling | **~$78** | **~$236–308** |
| **AWS Fargate** | Per-task: $0.04048/vCPU-hr + $0.004445/GB-hr | Good (no node ops; needs custom autoscaling) | **~$96** | **~$218–320** |
| **Azure AKS** | Free control plane (no SLA) / $72 Standard + VM nodes | Excellent | **~$88** | **~$280–350** |
| **AWS EKS** | $73/mo control plane + EC2 nodes | Excellent | **~$159** | **~$300–373** |
| **Cloud Run** | Per-request / instance-based | ⚠️ **Mismatch for workers** | ~$30 (HTTP tier only) | ~$335 (all warm, wasteful) |

**Key takeaways:**
- **DOKS is the best value** (free control plane, cheap $12 LB, simplest K8s UX) — especially dev at ~$36.
- **GKE Autopilot is the best technical fit** — per-pod billing + KEDA scaling Celery on queue depth matches the bursty workload perfectly. First cluster's control-plane fee is waived.
- **Cloud Run + Celery = architectural mismatch.** Celery polls a broker; it isn't request-invoked. Pinning `min-instances ≥ 1` to keep workers warm negates Cloud Run's only advantage and is the priciest always-on option. Use it at most for the Next.js + FastAPI HTTP tier in a split deployment.
- **EKS/AKS** give full K8s but the **highest operational burden** (you own node pools, patching, upgrades). AKS is cheaper than EKS (free control-plane tier).

Sources: [Fargate pricing](https://aws.amazon.com/fargate/pricing/) · [Cloud Run pricing](https://cloud.google.com/run/pricing) · [GKE pricing](https://cloud.google.com/kubernetes-engine/pricing) · [DOKS pricing](https://docs.digitalocean.com/products/kubernetes/details/pricing/) · [EKS/AKS/GKE 2026 comparison](https://spendark.com/blog/eks-vs-aks-vs-gke-pricing/)

---

## 6. Add-on costs (apply on top of ANY compute choice)

### 6a. MongoDB Atlas (managed) — vector search included free on all tiers

| Tier | Price | RAM/Storage | Vector Search |
|---|---|---|---|
| **M0 (free)** | $0 | 512 MB / 5 GB | ✅ |
| **Flex** | $8–$30/mo (capped at $30) | scales by ops/sec | ✅ |
| **M10 (first dedicated/prod)** | ~$57/mo | 2 GB / 10 GB | ✅ |
| **Dedicated Search Nodes** | per-node-hour (optional, M10+) | isolates search | ✅ |

- **Dev:** keep the self-hosted `mongodb/mongodb-atlas-local` container ($0) or use Atlas **M0** (free).
- **Small prod:** **Atlas Flex ($8–$30/mo)** is the sweet spot — managed backups + HA + vector search for less than self-managing.
- Self-hosting on the VM is $0 in licensing but costs you backups, HA, and 1–2 GB RAM for `mongot`.

### 6b. Managed Redis (Celery broker)

| Provider | Cheapest paid | Celery note |
|---|---|---|
| **AWS ElastiCache** `cache.t4g.micro` | **~$12/mo** | ✅ Best predictable broker, no per-command billing |
| **Upstash Fixed** | $10/mo (250 MB) | ✅ Use **Fixed**, never PAYG |
| **Upstash PAYG** | per-command | ❌ **Cost trap** — idle Celery polling = 10M+ cmds/mo |
| **Redis Cloud Essentials** | $18/mo (1 GB) | ✅ |
| **DO Valkey** | $15/mo (1 GiB) | ✅ Flat |

- **Dev:** self-hosted Redis container ($0) or Upstash free tier (256 MB).
- **Small prod:** **ElastiCache t4g.micro (~$12/mo)** — avoids Celery's aggressive-polling per-command trap.

### 6c. LLM API usage (the cost that usually dominates)

Per-million-token pricing (input / output), current 2026:

| Provider / model | Input $/1M | Output $/1M |
|---|---|---|
| **Claude Opus 4.8** | $5.00 | $25.00 |
| **Claude Sonnet 4.6** | $3.00 | $15.00 |
| **Claude Haiku 4.5** | $1.00 | $5.00 |
| OpenAI GPT-5 | $1.25 | $10.00 |
| OpenAI GPT-5 Mini | $0.25 | $2.00 |
| OpenAI text-embedding-3-small | $0.02 | — |
| Gemini 3.1 Pro | $2.00 | $12.00 |
| Gemini 3 Flash | $0.50 | $3.00 |
| Gemini Embedding | $0.15 | — |

**Scenario:** 50 textbook PDFs ingested + 1,000 questions generated + 5,000 answers marked per month ≈ **~32 M input / ~8.9 M output tokens + 6 M embedding tokens**.

| Routing strategy | **Monthly LLM cost** |
|---|---|
| **LOW** — Haiku 4.5 / Gemini Flash-Lite / GPT-5 Mini everywhere | **~$26** |
| **MEDIUM** — Sonnet 4.6 for gen+marking, Gemini Flash vision | **~$160** (≈ $90 with Batch API) |
| **HIGH** — Opus 4.8 / GPT-5 / Gemini Pro everywhere | **~$340** |

- **Answer marking dominates** (highest volume × tokens). Routing bulk marking to a mid-tier model is the single biggest lever.
- **Batch APIs cut ~50%** on ingest/marking; **Anthropic prompt caching cuts ~90%** on the repeated rubric/system prompt.
- Embeddings are negligible (~$0.12–$0.90/mo).

Sources: [MongoDB pricing](https://www.mongodb.com/pricing) · [Atlas Flex costs](https://www.mongodb.com/docs/atlas/billing/atlas-flex-costs/) · [Upstash Redis pricing 2026](https://upstash.com/blog/redis-pricing-comparison-every-major-provider-in-2026-with-numbers) · [ElastiCache pricing](https://aws.amazon.com/elasticache/pricing/) · Claude pricing (Anthropic, claude-api reference) · [OpenAI pricing](https://openai.com/api/pricing/) · [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing)

---

## 7. All-in master comparison (compute + Mongo + Redis, excl. LLM)

Small-production, realistic totals. LLM usage (~$26–$340) is **additional** on every row.

| Rank | Approach | Compute | + Mongo | + Redis | **All-in infra/mo** | Ops burden | Autoscaling |
|---|---|---|---|---|---|---|---|
| 🥇 | **Hetzner VM (16 GB), self-hosted Mongo+Redis** | ~$36 | $0 | $0 | **~$36** | High | Manual only |
| 🥈 | **Fly.io PaaS** + Atlas Flex + Upstash | ~$115 | ~$20 | ~$10 | **~$145** | Low | Per-machine |
| 🥉 | **DOKS** + Atlas Flex + DO Valkey | ~$110 | ~$20 | $15 | **~$145** | Medium | KEDA (best) |
| 4 | **Railway PaaS** + Atlas Flex + Railway Redis | ~$155 | ~$20 | ~$5 | **~$180** | Low | Usage-based |
| 5 | **Hetzner VM** + Atlas Flex + ElastiCache | ~$36 | ~$20 | ~$12 | **~$68** | Medium | Manual |
| 6 | **GKE Autopilot** + Atlas Flex + Memorystore | ~$270 | ~$20 | ~$15 | **~$305** | Medium | KEDA (best) |
| 7 | **DO App Platform** + Atlas Flex + Valkey | ~$230 | ~$20 | $15 | **~$265** | Low | None |
| 8 | **Render** + Atlas Flex + Key Value | ~$245 | ~$20 | $10 | **~$275** | Low | None |
| 9 | **AWS EC2** + Atlas + ElastiCache | ~$191 | ~$20 | ~$12 | **~$223** | High | Manual |
| 10 | **AWS Fargate / EKS** + Atlas + ElastiCache | ~$270–370 | ~$20 | ~$12 | **~$300–400** | Med–High | Yes |

> Row 5 (Hetzner VM + managed Mongo + managed Redis) is the **best reliability-per-dollar hybrid**: ~$68/mo gets you a cheap compute box while offloading the two stateful, failure-prone dependencies to managed services.

---

## 8. Recommendations

**For dev / staging / demo → Oracle Free or Hetzner CX23 (~$0–8/mo).**
Run the whole `docker compose` stack on one box. Self-host Mongo (atlas-local) + Redis. LLM cost is tiny during dev. Drop `mongo-express` and `flower` if you want to trim.

**For small production, cost-first → Hetzner VM + managed deps (~$68/mo infra).**
A 16 GB Hetzner CAX31 running the stack, but move MongoDB to **Atlas Flex** and Redis to **ElastiCache t4g.micro** so the two stateful services have managed backups/HA. Best reliability-per-dollar. Accept manual vertical scaling.

**For small production, ops-first → Fly.io or DOKS (~$145/mo infra).**
If you don't want to babysit a server: **Fly.io** for the simplest push-to-deploy at lowest PaaS cost, or **DOKS** if you want real Kubernetes + KEDA autoscaling on Celery queue depth. Both with Atlas Flex + managed Redis.

**For scale / bursty autoscaling → GKE Autopilot + KEDA.**
When queue depth varies wildly and you need workers to scale 0→N automatically, GKE Autopilot's per-pod billing + KEDA is the best technical fit (~$305/mo infra). Overkill below that.

**Avoid for this workload:** Cloud Run for the worker fleet (architectural mismatch), Upstash PAYG as the Celery broker (per-command cost trap), AWS EC2 with high egress, and Render's flat tiers (most expensive PaaS for 11 always-on services).

### The decision in one line
> **Cheapest:** Hetzner VM (~$36). **Best balance:** Hetzner VM + managed Mongo/Redis (~$68). **Lowest-ops:** Fly.io / DOKS (~$145). **Best autoscaling:** GKE Autopilot (~$305). On every option, **LLM API usage ($26–$340/mo) is the variable that will actually dominate your bill** — optimize model routing (Sonnet/Flash + Batch API) before optimizing infra.
