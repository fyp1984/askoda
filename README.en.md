# Askoda · Intelligent Data Requirement Assistant

> **English | [中文](README.md)**
>
> Askoda translates natural-language data requests into **well-calibrated, explainable, auditable SQL**.
> Every data fetch leaves a complete, traceable chain of judgment.

---

<p align="center">
  <a href="#1-project-positioning">Positioning</a> ·
  <a href="#2-core-features">Features</a> ·
  <a href="#3-architecture--design-principles">Architecture</a> ·
  <a href="#4-directory-structure">Structure</a> ·
  <a href="#5-quick-start">Quick Start</a> ·
  <a href="#6-verification--self-certification">Verification</a> ·
  <a href="#7-milestones--acceptance-matrix">Milestones</a> ·
  <a href="#8-contributing">Contributing</a> ·
  <a href="#9-roadmap">Roadmap</a> ·
  <a href="#10-license">License</a>
</p>

---

## 1. Project Positioning

Askoda is built for **highly-regulated industries** (finance, energy, government, large manufacturing, etc.) where data retrieval must be defensible. It does not simply "translate questions into SQL" — it enforces an auditable judgment chain for every fetch:

> How was the request understood → Which table was used → Where did the metric definition come from → What did each gate block → Who executed it and when → Why was the result deemed deliverable.

Askoda is **not a "universal AI chat-over-data" tool**. When definitions are ambiguous, one-to-many amplification is possible, fields are unmodeled, or metrics are not yet confirmed — Askoda **returns clarifying questions or refuses entirely** rather than guessing with silent defaults. This is the core difference from general AI-BI tools on the market.

**When to use Askoda**
- SQL production workflows where analysts repeatedly align definitions with business stakeholders
- Regulatory reporting / internal audit / compliance teams requiring immutable data provenance
- Large enterprises with complex metrics and imperfect historical data governance
- Data platform teams rolling out a "semantic layer + evidence chain" foundation incrementally

**When NOT to use Askoda**
- Casual ad-hoc analysis on small personal datasets (ChatBI tools are faster)
- Pure product demos that demand "one sentence → instant chart" (Askoda will ask follow-ups, not jump to results)

---

## 2. Core Features

| Feature | Description |
|---|---|
| **Requirement Intake & Structuring** | One business sentence → structured requirement (metric / dimension / filter / granularity / time window) with JSON Schema validation |
| **Semantic-Layer Constrained Generation** | No free-form SQL. Candidate objects are narrowed to the semantic layer (MDL) closed set; misses are rejected rather than guessed |
| **5-Layer SQL Gate** | L1 syntax (sqlglot) → L2 AST static rules → L3 read-only gate → L4 semantic dry-plan → L5 result assertion |
| **Clarification Q&A Closed Loop** | Generates clarifying questions for ambiguous definitions; answers are versioned and traced. Never silent defaults |
| **Read-Only Execution** | All SQL runs through the semantic layer; no write operations on the physical layer. `DELETE`/`INSERT`/multi-statement attacks are blocked at L3 |
| **9-Field Traceability + Audit Replay** | Every run records requirement version, semantic layer version, rules version, generated SQL, gate result, actor … Full 5-segment replay supported |
| **Dual Dataset Swap** | Same code runs against two semantic layers (raw dataset A / governed dataset B) with **zero code change** |
| **Failure Fallback Matrix F1–F5** | Classifies failures into unambiguous buckets (ambiguous definition / missing metadata / candidate conflict / rule-blocked / execution error) so you know *what to fix* |
| **Evidence Orchestration + Deterministic Rules** | **No LLM in the gateway.** Semantic analysis is driven by evidence priority (P1–P9) + ruleset (R1–R7); every conclusion carries its source and confidence |
| **Robustness & Concurrency Safety** | 3 backoff retries for read-only tools; layered timeout config (dataset-specific > global > 60s default); unique `run_id` within a process; core chains use only local variables |
| **Knowledge Citation Trace** | Every knowledge hit is persisted in `knowledge_citations` (KC- prefix) with exact doc/chunk coordinates for audit replay |
| **Ingress-Side PII Masking** | Auto-scans 7 categories of sensitive data (name / mobile / ID card / bank card / email / address / amount) at submission; masked and raw versions are stored separately with reveal-audit logs |

---

## 3. Architecture & Design Principles

### 3.1 Overview

```
Clients (any agent: Doubao / WorkBuddy / Codex …)
        │
        ▼
Custom Frontend ──► FastAPI Gateway / BFF ──► MCP Gateway (FastMCP 4.x, streamable-http)
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                     ▼                     ▼
                  Wren Semantic A        Wren Semantic B     Knowledge / Metadata DB
                  (raw e-commerce)     (governed retail)    (RAGFlow IR + MinIO)
```

### 3.2 Three Non-Negotiables

1. **No LLM inside the gateway.**
   Semantic analysis runs on "evidence orchestration + deterministic rules". Every conclusion carries provenance and confidence. Open-ended generation (conversational completion, business tone polishing) is **explicitly delegated to client-side agents**.
   As a result, this project **does not bind to any specific LLM provider**. Post-M3 acceptance and replay run on any agent.

2. **No forks, no patches to upstream engines.**
   All customisation goes through official extension points or adapter layers. Wren Engine Ibis uses the upstream image; SQL parsing uses sqlglot's pure-Python public API. Vendor source code is never touched.

3. **All 3rd-party capability is funneled through single modules.**
   Gates → `gateway/gates.py`; Contracts → `gateway/contracts/`; Rules → `gateway/rules.py`; Evidence → `gateway/evidence.py`.
   Swapping an underlying engine changes one file, not every call site.

### 3.3 The Fetch Judgment Chain (5 Segments · Recoverable via Audit Replay)

```
① input      Structured requirement (6 slots + 4 version IDs: schema/requirement/pack/rules)
    │
    ▼
② sql        plan() → metadata draft (no SQL body) → generate() → produce SQL → immediate gate review
    │
    ▼
③ review     L1–L5 gate cascade; every rule hit records snippet + detail
    │
    ▼
④ result     Read-only execution outcome (row_count / columns / sample / signal columns)
    │
    ▼
⑤ knowledge  Knowledge citations (KC- prefix + chunk_id + doc coordinates + snippet)
```

Any segment failure is classified via `fallback.py` F1–F5; nothing silently proceeds.

---

## 4. Directory Structure

```
askoda/
├── gateway/          MCP server (41 tools): intake / analysis / generate / gate / run / trace
│   ├── contracts/    Public contracts (structured requirement JSON Schema …)
│   ├── Dockerfile
│   ├── requirements.txt   Dependencies only: fastmcp / psycopg / minio / sqlglot
│   ├── app.py        FastMCP entrypoint
│   ├── wren.py       Wren streamable-http client (session reuse + timeouts + retries)
│   ├── db.py         Gateway metadata DB: 7 tables DDL + query/query_one/execute API
│   ├── demand.py     Requirement intake: 4 validations + state machine + event trace
│   ├── masking.py    Ingress PII masking: 7 rules + surname guard
│   ├── knowledge.py  RAGFlow client: citation parsing + compressed-query fallback
│   ├── metadata.py   Metadata glossary: MDL × physical schema merge, "table exists but column unmodeled" tagging
│   ├── collect.py    Metadata collection: native / schemacrawler dual backends
│   ├── attachments.py  MinIO attachments: upload / list / pre-signed download URLs
│   ├── evidence.py   Evidence orchestration: P1–P9 ladder + lexicon + conflict detection
│   ├── rules.py      L2 static rules: R1–R7, pure-function module
│   ├── gates.py      5-layer gate dispatcher: L1→L2→L3→L4→L5
│   ├── planner.py    Deterministic planner fallback
│   ├── requirement.py  Structured requirement validation + versioning
│   ├── semantics.py  Semantic analysis: 6-slot deterministic resolution, pure no-IO
│   ├── analysis.py   Analysis orchestration + persistence: rounds + Q&A versions
│   ├── fallback.py   F1–F5 failure classification: pure, no exceptions
│   ├── planner.py / sqlgen.py / sqlpack.py   2-stage SQL generation (plan → generate)
│   ├── sqlrun.py     Read-only execute + 9-field trace + list filter + audit replay
│   └── healthcheck.py  Dual liveness (HTTP /healthz + MCP initialize/tools-list)
├── tools/            Self-cert & independent re-scan scripts (per-milestone suites + adversarial cases)
│   ├── m4_verify.py / m5_verify.py / mcp_acceptance_check.py …
│   ├── gates_selftest.py / rules_selftest.py / evidence_selftest.py …
│   ├── audit_verify.py (audit replay) / robustness_verify.py (retries/timeouts/concurrency)
│   └── collect_metadata.py / reset_demo_data.py
├── poc-eval/         POC evaluation & E2E drills (question banks, replay, scripts, machine-readable reports)
│   ├── poc_e2e_gateway.py    Mainline stories + POC-1 bank + POC-3 traps + MDL fact-check
│   └── e2e-gateway-report.json
├── knowledge/        Example knowledge-base documents
│   ├── 01-Metric-Caliber-Handbook-Retail-Member-Domain.md
│   ├── 02-Data-Security-and-Sensitive-Field-Management-Policy.md
│   ├── 03-Table-Structure-and-Granularity-Notes-Retail-Member-Domain.md
│   └── 04-Requirement-Intake-and-Caliber-Confirmation-Protocol.md
├── wren-docker/      Mock dataset A (e-commerce: 6 models / 29 fields / 5 relationships)
├── wren-docker-b/    Mock dataset B (retail member: 8 models / 47 fields / 10 relationships)
├── demo/             Demo artefacts
│   ├── demo-server.py       Local live-demo server (127.0.0.1:8080)
│   ├── Data-Requirement-Assistant-V7.html  Workbench demo page
│   └── Semantic layer live pages + brand assets
├── docker-compose.yml      One-click orchestration (gateway + 2 datasets + metadata DB + MinIO = 7 services)
├── .env.example  Environment variable template
└── README.en.md    This file
```

> Project-management artifacts (PRD, technical solution, battle plans, acceptance sheets, archives) are **kept outside this repository** in the project documentation workspace. This repository contains engineering files and developer-facing instructions only.

---

## 5. Quick Start

### 5.1 Prerequisites

- Docker (OrbStack / Docker Desktop 4.28+ recommended)
- Python 3.11+ (only for running local tool scripts; not needed to operate the server)
- Docker Compose V2 (`docker compose` subcommand available)

### 5.2 3-Step Boot

```bash
# 1. Configure environment
cp .env.example .env

# 2. One-click start all services (first run builds the gateway image, ~1–3 min)
docker compose up -d

# 3. Check status — all 7 services must be healthy
docker compose ps
```

Rebuild after changing `gateway/` source:

```bash
docker compose up -d --build
```

> ⚠️ macOS + OrbStack sandbox note: `compose build` needs buildx config relocated into the repo (sandbox blocks `~/.docker/buildx` writes):
> ```bash
> BUILDX_CONFIG="$PWD/.buildx" docker compose build gateway
> ```

### 5.3 Interfaces

| Item | URL | Notes |
|---|---|---|
| **MCP Endpoint** | `http://127.0.0.1:18080/mcp` | streamable-http, PROTOCOL_VERSION=2025-03-26 |
| **Health Check** | `http://127.0.0.1:18080/healthz` | Per-dataset/component status; if `degraded`, inspect the `components` field |
| **Mock Dataset A (E-commerce)** | `127.0.0.1:9000`(MCP) / `15432`(PG) | 6 models / 29 fields / 5 relationships |
| **Mock Dataset B (Retail Member)** | `127.0.0.1:9002`(MCP) / `15433`(PG) | 8 models / 47 fields / 10 relationships |
| **Gateway Metadata DB** | `127.0.0.1:15434`(PG) | user=assistant / requirements, events, metadata glossary |
| **Attachment Storage** | `127.0.0.1:19000`(S3) / `19001`(Console) | MinIO, bucket `demand-attachments` |
| **Knowledge Base (optional)** | `http://127.0.0.1:19380/api/v1` | RAGFlow v0.26.4 IR API; gateway acts only as a client |

Gateway host port is `18080` (not `8080`) because port `8080` is reserved for the local demo server `demo/demo-server.py`.

### 5.4 Troubleshooting FAQ

| Symptom | Root Cause | Fix |
|---|---|---|
| First call after `assistant-postgres` boot fails on missing tables | `init_schema` runs idempotently at gateway startup; PG not ready yet | `docker compose restart gateway`; health check will flip `degraded` back to `ok` |
| Wren MCP returns `503` / `connection refused` | Wren Engine cold-start takes 30–60s on first boot | Wait 60s and retry, or `docker compose logs wren-mcp-a` |
| Knowledge API reports `host.docker.internal` unreachable | RAGFlow not running on host or different network | Set actual reachable `KNOWLEDGE_API_URL` in `.env`, or leave blank — `degraded_sources` will report honestly |
| Volume `wren-pgdata` says `external volume not found` | Fresh host, no legacy volume from single-stack era | Change `WREN_PG_VOLUME=askoda_wren-pgdata` in `.env` or drop `external: true` to let compose manage it |
| `tools/*_verify.py` raises ModuleNotFoundError | Script imports gateway internals; cannot run bare-metal on host | Follow script header comments: `docker cp` into container + run with `PYTHONPATH=/app` |

---

## 6. Verification & Self-Certification

All verification scripts honour a unified contract:
- **Exit 0 = all green; non-zero = assertion failed; exit 2 = data inconsistency**
- **No external LLM dependency**; all suites reproduce fully offline
- **Self-check ≠ acceptance**: critical chains (e.g. M3 tech-word gate) independently re-scan at the client side rather than trusting server-side lists

### 6.1 Core Verification (runs on host)

```bash
# Dual liveness probe (HTTP + MCP full handshake). Add --deep to actually execute gateway_health.
python3 gateway/healthcheck.py --base http://127.0.0.1:18080 --deep

# MCP tool surface acceptance (41 tools + real-call match against Implementation & Acceptance Plan)
python3 tools/mcp_acceptance_check.py

# 5-layer gate self-cert (fully offline, no DB)
python3 tools/gates_selftest.py
python3 tools/rules_selftest.py

# Masking / IR / Evidence / Semantics — four self-cert suites (most run offline)
python3 tools/masking_selftest.py
python3 tools/knowledge_selftest.py
python3 tools/evidence_selftest.py
python3 tools/semantics_selftest.py
```

### 6.2 Container-Only Scripts (require gateway internals)

```bash
# Example: M5 gate E2E, 62 assertions
docker cp tools/m5_verify.py demand-gateway:/app/tools/m5_verify.py
docker exec -e PYTHONPATH=/app demand-gateway python /app/tools/m5_verify.py

# M6-4 audit replay assertions (dataset back-ref + payload column correctness)
docker cp tools/audit_verify.py demand-gateway:/app/tools/audit_verify.py
docker exec -e PYTHONPATH=/app demand-gateway python /app/tools/audit_verify.py

# M6-3 robustness (timeout backoff retries + 20-concurrent run_id uniqueness)
docker cp tools/robustness_verify.py demand-gateway:/app/tools/
docker exec -e PYTHONPATH=/app demand-gateway python /app/tools/robustness_verify.py
```

### 6.3 End-to-End Drill

```bash
python3 poc-eval/poc_e2e_gateway.py
# Produces poc-eval/e2e-gateway-report.json (machine-readable, archive-grade)
```

Covers: 12 mainline stories + 30-item POC-1 bank (20 positive / 10 refusal) + 10-item POC-3 trap suite + 50-item MDL fact-check.

### 6.4 Pre-Demo Environment Reset

```bash
# Default dry-run; without --apply nothing is touched
python3 tools/reset_demo_data.py

# Wipe requirements / analysis rounds / confirmations / knowledge test fixtures
python3 tools/reset_demo_data.py --apply

# Also purge attachment objects
python3 tools/reset_demo_data.py --apply --purge-attachments
```

---

## 7. Milestones & Acceptance Matrix

### 7.1 Completed Milestones (M1–M6)

| Milestone | Deliverables | Acceptance Script | Result (real measured values) |
|---|---|---|---|
| **M1** Gateway Skeleton | 7 MCP tools + dual Wren dataset + deterministic ask fallback | `mcp_acceptance_check.py` M1 block | 7/7 ✅ |
| **M2** Requirement Intake | 14 tools: demand CRUD / masking / metadata / attachments / knowledge | `m2_verify.py` | 22/22 ✅ |
| **M3** Semantics + Clarification Loop | Evidence P1–P9 + Rules R1–R7 + 7 tools | `m3_verify.py` | 109/109 ✅ |
| **M4** 2-Stage SQL Generation | plan() → generate() + closed-set self-check + pack_version hash | `m4_verify.py` | 51/0 ✅ (dataset A) |
| **M5** 5-Layer Gate + Pure Rule Base | sqlglot + R1–R7 canonicalisation + read-only + dry-plan + result assert | `m5_verify.py` | 62/0 ✅ (dataset B) |
| **M6-1** Rule Version Fingerprints | `rules_version` embeds `source_sha8` of each `evaluate` | `m61r2_version_check.py` | ✅ |
| **M6-2** Fallback Matrix F1–F5 | Pure classification + F2 criterion fixed to (empty `chosen_table`) | `m62_live_check.py` + `fallback_verify.py` | ✅ |
| **M6-3** Robustness (timeout/retry/concurrency) | 3-tier timeout config + READ_TOOLS 3× backoff + unique run_id | `robustness_verify.py` | 18/18 ✅ |
| **M6-4** Audit Trace + Replay | actor pass-through + knowledge_citations + dataset-backed list filter + 5-segment replay | `audit_verify.py` | Section B 16/16 ✅ |

### 7.2 POC Question Bank Acceptance

| Bank | Positive Precision | Refusal Coverage | POC-3 Trap Intercept Rate |
|---|---|---|---|
| POC-1 (30 items) | 20/20 = 100% | 10/10 = 100% | — |
| POC-3 (10 items) | — | — | 7/10 = 70% (rule hardening in progress) |

### 7.3 Known Tech Debt (Non-Blocking)

- [ ] `docker-compose.yml` tags `image: demand-assistant-gateway:0.1.0` while code reports version 0.3.0
- [ ] `.buildx/` directory not declared in `.gitignore` (OrbStack sandbox-specific)
- [ ] POC-3 3/10 misses: R4 multi-value ordering, R6 time-column suffix blacklist expansion, R7 cross-model unmodeled-field reference
- [ ] RAGFlow table-derived chunks have empty `positions`; exact coordinates need upstream RAGFlow upgrade

---

## 8. Contributing

### 8.1 Commit Convention (Conventional Commits + DCO Signoff)

```
<type>(<scope>): <subject>

<body>

Signed-off-by: 西北人 <fyp1984@yeah.net>
```

- `type` ∈ `feat / fix / docs / style / refactor / perf / test / chore / revert`
- `scope` = `gateway / tools / poc-eval / demo / wren-docker / wren-docker-b / docs`
- **Every commit MUST carry DCO `Signed-off-by`** (use `git commit -s`)
- One logical change per commit; code and docs commits are separate

### 8.2 Hard Constraints (Fail CI)

| ID | Constraint | Enforced By |
|---|---|---|
| HC-01 | No LLM calls inside the gateway | `grep -r "openai\|anthropic\|gemini\|llm\|chat_" gateway/` must be empty |
| HC-02 | No patches to 3rd-party open-source engine source | Manual review |
| HC-03 | 3rd-party deps limited to `fastmcp / psycopg / minio / sqlglot` | `gateway/requirements.txt` line count frozen (bugfix version pins OK) |
| HC-04 | All DB access through `gateway/db.py` `query/query_one/execute` | Call-site audit |
| HC-05 | `gateway/rules.py` must remain pure: zero IO / zero DB / zero network | Import audit |
| HC-06 | Every `.py` file must pass `ast.parse` | `python3 -c "import ast; ast.parse(open(f).read())"` per file |
| HC-07 | All 9 trace fields mandatory, no omitted rows | `m5_verify.py` / `audit_verify.py` assertions |
| HC-08 | `actor` passed through into `sql_runs.actor`; never defaulted | `audit_verify.py` Section B |
| HC-09 | On filter error, NEVER silently degrade to unfiltered list | `audit_verify.py` dataset 3-state comparison |

### 8.3 3-Step Acceptance For New Features

1. **Write the self-cert script first**, drop it at `tools/mXX_<feature>_selftest.py`, all assertions, no deps on new code;
2. **Then implement**, first run `ast.parse` sweep;
3. **Finally run regression** (`m4_verify` / `m5_verify`) to confirm 51/0 and 62/0 baselines do not regress.

---

## 9. Roadmap

### Short-Term (v0.4 · Scoped)

- [ ] **M6-5**: Multi-candidate SQL divergence (F3 factor threshold 0.35) + hold-state manual review workbench
- [ ] **M6-6**: R4/R6/R7 rule hardening, POC-3 trap intercept target 70% → 90%
- [ ] **M7**: Conversational requirement completion (client agent multi-turn, no gateway core changes)
- [ ] **M8**: Metric-caliber glossary online management UI (current path: knowledge docs + metadata dict)

### Mid-Term (v0.5 · Planning)

- [ ] Semantic layer MDL online editor + PR-style change approval flow
- [ ] First-pass auto-rewrite after gate block (governance hint `how_to_fix` → patch SQL)
- [ ] Dataset registration API (currently code-registry based via `gateway/registry.py`)
- [ ] FastMCP 5.x upgrade + official `create_proxy` return (drop handwritten Wren client)

### Long-Term (v1.0 · Concept)

- [ ] Multi-tenancy + dataset-level fine-grained RBAC
- [ ] Metric lineage visualisation (demand → SQL → table/field → caliber doc trace graph)
- [ ] Export integrations with mainstream BI (Metabase / Superset / Tableau)
- [ ] Turnkey on-prem installers (helm chart + offline bundle)

---

## 10. Security Policy

**DO NOT file GitHub Issues for security bugs.**

Mail maintainers directly at `<fyp1984@yeah.net>` within 24 hours for any of the following classes:

- SQL injection bypass (L3 read-only gate defeated)
- Multi-statement concatenation escape past L2/L3
- Masking under-reporting (real PII such as mobile/ID/bank-card persisted unmasked)
- Attachment storage permission bypass (download attachments not belonging to your demand_id)
- Knowledge-base credentials leaked to logs or response bodies
- Actor forgery (execution recorded under different actor than authenticated caller)

Please attach a minimal reproduction script and redacted request/response bodies.

---

## 11. License

© 2026 CheersAI · 西北人

Source code in this repository is licensed under the **MIT License** as declared in each file header; **documentation sections** are licensed under CC BY 4.0 with attribution required.

Third-party dependency licenses:
- [FastMCP](https://github.com/jlowin/fastmcp) — MIT
- [sqlglot](https://github.com/tobymao/sqlglot) — MIT
- [Wren Engine Ibis](https://github.com/Canner/wren-engine) — Apache-2.0
- [PostgreSQL](https://www.postgresql.org) — PostgreSQL License
- [MinIO](https://github.com/minio/minio) — AGPL-3.0 (used as independent service; no static/dynamic linkage with Askoda code)
