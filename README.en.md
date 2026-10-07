# Askoda · Intelligent Data Requirement Assistant

> **English | [中文](README.md)**

<p align="center">
  <b>A natural-language & business-intent-driven analytics and retrieval tool for the enterprise</b><br/>
  Ontology-driven · Intent-to-Analytics
</p>

**In one sentence:** ask in plain business language, and Askoda returns a **well-calibrated, explainable, auditable** answer — every fetch leaves a complete, traceable chain of judgment.

Its full chain has four stages:

> **① Knowledge base (upstream)** → **② Requirement understanding** → **③ Constrained execution (safety + accuracy gates)** → **④ Compliance audit (downstream)**

The fundamental difference from a plain "intent → SQL" tool lies in the **outer two stages**: **build the knowledge base before the question, run the compliance audit after the fetch** — those two stages are what make it deployable in real enterprise production.

---

<p align="center">
  <a href="#1-project-positioning">Positioning</a> ·
  <a href="#ontology">Ontology-Driven</a> ·
  <a href="#2-core-features">Features</a> ·
  <a href="#3-architecture--design-principles">Architecture</a> ·
  <a href="#4-directory-structure">Structure</a> ·
  <a href="#5-quick-start">Quick Start</a> ·
  <a href="#mcp-tools">MCP Capability Map</a> ·
  <a href="#mcp-ops">MCP Ops & Upgrade</a> ·
  <a href="#6-verification--self-certification">Verification</a> ·
  <a href="#7-milestones--acceptance-matrix">Milestones</a> ·
  <a href="#8-contributing">Contributing</a> ·
  <a href="#9-roadmap">Roadmap</a> ·
  <a href="#11-license">License</a>
</p>

---

## 1. Project Positioning

Askoda is a **natural-language & business-intent-driven analytics and retrieval tool for the enterprise**, built ontology-first. It is **not** limited to highly-regulated retrieval (finance, energy, government, large manufacturing, etc.) — it targets **general enterprise analytics and retrieval in real production**.

Its fundamental difference from typical "intent → SQL" tools: **it builds a knowledge base *before* requirement analysis, and performs compliance audit *after* data extraction** — these two outer stages are what make it deployable in real enterprise production.

> The full judgment chain for one question: How was the request understood → Which table was used → Where did the metric definition come from → What did each gate block → Who executed it and when → Why was the result deemed deliverable.

### 📖 Glossary

Abbreviations (**MDL**, **BFF**, **MCP**, **RAGFlow**, **TEI**, **Wren**) and domain terms (evidence levels P1–P9, semantic rules R1–R7, static rule REGISTRY, gate layers G0–G3, architecture red lines R1–R5) are consolidated in one place:

**→ [术语表 · docs/GLOSSARY.md](docs/GLOSSARY.md)** *(Chinese)*

> ⚠️ **Four independent numbering systems share similar labels**: `R1–R5` = architecture red lines, `R1–R7` = requirement semantic rules, `P1–P9` = evidence levels, `G0–G3` = gate layers. Always spell out the full name when referring to them.

Askoda is **not a "universal AI chat-over-data" tool**. When definitions are ambiguous, one-to-many amplification is possible, fields are unmodeled, or metrics are not yet confirmed — Askoda **returns clarifying questions or refuses entirely** rather than guessing with silent defaults. This is the core difference from general AI-BI tools on the market.

### 1.1 The 3-Layer Spine: Facts → Logic → Action

<a id="ontology"></a>

Askoda is designed around the idea of **ontology-driven data management** (theoretical reference: *Ontology-Driven AI Data Management*, compiled by an editorial board, China Machine Press, 1st ed., May 2026, ISBN 978-7-111-81075-9). A single data fetch is decomposed into three layers, bottom-up:

| Layer | What it carries | In one line |
|---|---|---|
| **① Facts Layer** | Requirement intake · Metadata collection & dictionary · Attachment archive · Schema snapshot · Ingress masking | Turn raw requirements and metadata into governed **facts** |
| **② Logic Layer · Ontology Knowledge Base** | MDL semantic layer · Knowledge retrieval · Evidence orchestration · Business ruleset · Clarification Q&A | Distill business semantics and expert knowledge into the **ontology knowledge base** |
| **③ Action Layer** | Two-stage generation · 5-layer gate · Read-only execution · 9-field trace · Audit replay | **Constrain-generate** queries and analysis from one natural-language request |

The three layers are **directionally dependent and cannot be skipped**: no clean facts, no distillation; no confirmed definitions, no generation. This is the root reason Askoda asks follow-ups instead of falling back to silent defaults.

### 1.2 Where the Six Ontology Primitives Live

| Ontology primitive | Carrier in Askoda |
|---|---|
| Class | MDL models (dataset A: 6 / dataset B: 8) |
| Instance | Rows returned by read-only execution (`SELECT` / `WITH` only) |
| Property | MDL fields + metadata dictionary (business meaning / aliases; unmodeled fields explicitly marked "invisible to AI") |
| Relation | MDL `relationships` (A: 5 / B: 10) |
| Constraint | Enum domains · granularity · field visibility · L3 read-only boundary |
| Rule | L2 static ruleset (11 rules, 4 of them blocking) + L4 semantic dry-plan |

### 1.3 Minimal Facet: Don't Build Everything, Ship One Slice

An ontology is **grown, not built in one shot**. Askoda advances in `point → line → surface` slices, each verifiable on the spot:

| Facet | Meaning | Measured in this project |
|---|---|---|
| **Point** | One table + one definition | ≤ 2 person-days per table (dim 0.5 / aggregate 1 / detail-wide 1.5–2) |
| **Line** | A cluster of strongly related tables + shared definitions + join direction | Trade-domain chain (orders → items → refunds) landed in one pass; L2 resolves JOIN direction |
| **Surface** | One data domain = one versionable MDL set | Datasets A / B coexist with **zero code change (diffs = 0)**; one full surface ≈ 8 tables · 47 fields · 9.5 person-days |

Where to cut first? Prefer real pain points that are **semantically complex, rule-explicit, and compliance-heavy**; casual exploratory analysis and "one-sentence-to-chart" demos are not a fit.

### 1.4 Boundary (Important)

In this version the **Action layer generates SQL**, with **MDL** as the semantic layer. **Ontology-native querying (graph query / Cypher) is a roadmap direction and is not implemented.** Askoda borrows the ontology philosophy and its layering (facts / logic / action, minimal facets) but does **not** ship an RDF/OWL store or a SPARQL / Cypher engine. Please do not present the roadmap as current capability.

**When to use Askoda**
- **Business operations / business analysts** querying in natural language and getting results (with proper permissions)
- Query-production workflows where analysts repeatedly align definitions with business stakeholders
- Regulatory reporting / internal audit / compliance teams requiring immutable data provenance
- Large enterprises with complex metrics and imperfect historical data governance
- Data platform teams rolling out a "knowledge base + semantic layer + evidence chain" foundation incrementally

**When NOT to use Askoda**
- Casual ad-hoc analysis on small personal datasets (ChatBI tools are faster)
- Pure product demos that demand "one sentence → instant chart" (Askoda will ask follow-ups, not jump to results)

### 1.5 The Full Chain & What Makes It Different

A single question is split into four stages. **The first and last are the watershed** between Askoda and ordinary "intent→SQL" tools (which usually do only the middle two):

| Stage | What happens | Key components |
|---|---|---|
| **① Knowledge Base (front)** | Knowledge aggregation → semantic analysis → knowledge management, distilled into a versionable ontology knowledge base | Facts layer (requirement / metadata / attachments / schema / masking) + Logic layer (MDL semantic layer / knowledge retrieval / rules) |
| **② Requirement Understanding** | Natural language / business intent → structured requirement + evidence orchestration + clarification loop | `requirement_structured` / `analysis_*` / `confirmation_*` |
| **③ Constrained Execution** | Two-stage generation → 5-layer gate (**safety + accuracy checks**) → read-only execution | `sql_plan` / `sql_generate` / `sql_review` / `sql_execute_readonly` |
| **④ Compliance Audit (back)** | 9-field traceability + audit replay + knowledge-citation provenance (**post-hoc audit hardening**) | `sql_run_*` / `knowledge_citations` |

| Dimension | Generic "intent→SQL" tool | Askoda |
|---|---|---|
| Starting point | Generate SQL directly | Build a **knowledge base** first |
| Semantics source | LLM guesses on the fly | **Ontology knowledge base**, deterministic alignment |
| After generation | Almost no validation | **5-layer gate** (syntax / static rules / read-only / semantic dry-plan / result assertion) |
| On error | Silently returns plausible-but-wrong numbers | **Asks or refuses** — no silent defaults |
| Provenance / compliance | Usually none | **Full-chain replay + post-hoc audit hardening** |
| Fit | Demos / personal exploration | Enterprise **production** |

**Who can use it**: with permissions in place, access is role-scoped — usable by both technical / data-engineering staff and by business operations / business analysts.

---

## 2. Core Features

| Feature | Description |
|---|---|
| **Ontology-Driven 3-Layer Spine** | Facts (requirement / metadata governance) → Logic (MDL semantic layer + expert knowledge distillation) → Action (constrained query generation); the layers are directionally dependent and cannot be skipped |
| **Knowledge Base (front)** | Before requirement analysis: knowledge aggregation → semantic analysis → knowledge management, distilled into a versionable ontology knowledge base (MDL + metadata dictionary + ruleset) |
| **Compliance Audit Hardening (back)** | 9-field traceability + audit replay + knowledge-citation provenance (`knowledge_citations`); compliance-facing post-hoc audit can be replayed end to end |
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
│   ├── rules.py      L2 static rules: 11 (4 blocking / 7 warn), pure-function module
│   ├── gates.py      5-layer gate dispatcher: L1→L2→L3→L4→L5
│   ├── planner.py    Deterministic planner fallback
│   ├── requirement.py  Structured requirement validation + versioning
│   ├── semantics.py  Semantic analysis: 6-slot deterministic resolution, pure no-IO
│   ├── analysis.py   Analysis orchestration + persistence: rounds + Q&A versions
│   ├── fallback.py   F1–F5 failure classification: pure, no exceptions
│   ├── sqlgen.py / sqlpack.py   2-stage SQL generation (plan → generate)
│   ├── sqlrun.py     Read-only execute + 9-field trace + list filter + audit replay
│   └── healthcheck.py  Dual liveness (HTTP /healthz + MCP initialize/tools-list)
├── tools/            Gate, self-test & ops tooling (shipped long-term)
│   ├── gate_all.py   Unified gate entry (G0–G3, auto-discovers sibling scripts)
│   ├── redline_guard.py  Machine-checked six red lines
│   ├── mcp_acceptance_check.py  Tool-surface verification over real MCP protocol
│   ├── *_selftest.py  Offline self-tests (gates / rules / masking / knowledge / evidence / semantics)
│   └── collect_metadata.py / ingest_knowledge.py / reset_demo_data.py / embedding_failover.sh
├── poc-eval/         POC evaluation & E2E drills (question banks + drill scripts)
│   ├── poc_e2e_gateway.py    Mainline stories + POC-1 bank + POC-3 traps + MDL fact-check
│   └── poc_eval.py / bank-b.json / rule_upgrade_dryrun.py
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
- **Tailscale** (only for knowledge retrieval, see 5.3; without it that capability is fully down)
- **Node.js 18+ / npm** (only if you modify `web/src/`, see 5.2; not needed to just run the stack)

### 5.2 3-Step Boot

```bash
# 1. Configure environment
cp .env.example .env

# 2. One-click start all services (first run builds the gateway image, ~1–3 min)
docker compose up -d

# 3. Check status
docker compose ps
```

**How to read step 3**: the `STATUS` column only shows `(healthy)` for services that **have a
healthcheck**. In this project **`wren-mcp-a`, `wren-mcp-b` and `assistant-minio` have no
healthcheck** (not defined in compose, and the images don't ship one), so they only ever show
`Up x days` — **that is normal and does not mean they failed to start**.
To check readiness, look at these instead: `gateway`, `bff`, `assistant-postgres`,
`wren-postgres-a` and `wren-postgres-b` should show `(healthy)`, and neither `gateway` nor
`bff` should be `Restarting`.

A more reliable check is to hit the gateway health endpoint directly (it actually walks both
dataset connections):

```bash
curl -s http://127.0.0.1:18080/healthz
```

`status=ok` means both datasets are reachable; if you get `degraded`, inspect the `components`
field to find which one is down.

> The service count varies with the profile in use (trust `docker compose config --services`):
> 8 by default; 9 with `--profile local-embed` (adds `tei-embedding`, the local backup embedding).

Rebuild after changing `gateway/` source:

```bash
docker compose up -d --build
```

> ⚠️ macOS + OrbStack sandbox note: `compose build` needs buildx config relocated into the repo (sandbox blocks `~/.docker/buildx` writes):
> ```bash
> BUILDX_CONFIG="$PWD/.buildx" docker compose build gateway
> ```

> **Frontend build artifacts are committed, so a fresh clone works out of the box — but any
> change under `web/src/` requires a rebuild to take effect.**
>
> The Vite output in `bff/static/` **is tracked in git** (`index.html` + `assets/*`), so you can
> open the frontend right after cloning without building anything. However, BFF only serves
> `bff/static/` verbatim — **it cannot and does not compile TypeScript**. Anything you change
> under `web/src/` stays invisible until you rebuild and overwrite `bff/static/` (the symptom is
> "I edited the code and the page didn't change").
>
> After editing `web/src/`:
>
> ```bash
> cd web && npm install && npm run build
> ```
>
> Vite writes the output into `bff/static/`; then just refresh the browser (in non-dev mode run
> `docker compose restart bff` so BFF re-reads the mount; in dev mode the whole directory is
> mounted, so a refresh is enough). This is why Node.js 18+ and npm are required locally —
> **but you don't need Node at all if you only run the stack without touching the frontend**.

### 5.3 External Dependencies & Known Limitations

**Read this before deploying on a new machine.** This project is *not* self-contained: the
knowledge-retrieval path depends on **an external host**, and **the knowledge backend is not yet
deployed as an independent stack**. Skipping this section leaves you with a system where
everything works except retrieval, which is fully down — with no error telling you what's missing.

#### 5.3.1 Remote embedding service (a hard dependency of knowledge retrieval)

| Item | Value |
|---|---|
| Address | `http://100.103.240.78:18001/v1` |
| Tailscale node | `sevensmile.tailb5e44d.ts.net` |
| Model | `bge-m3` (fixed, not swappable) |
| Consumed by | RAGFlow, which calls it per retrieval to embed the query |

**What happens when it's down**: knowledge retrieval is **fully down**, not degraded. RAGFlow
does not cache query vectors — it calls the embedding service on every `/api/v1/retrieval`
request — so if this address is unreachable, every retrieval request fails with
`EmbeddingError code=100` (surfacing as a total `knowledge_search` failure).

**Tailscale is a prerequisite**: this is a Tailscale-internal address, so **without a logged-in
Tailscale client it simply cannot be reached**. If a new machine has Tailscale installed but not
logged in, retrieval is guaranteed to fail — that's a network-layer problem, not a config typo.

Check connectivity first (depends on nothing in this project):

```bash
curl -s -m 8 --noproxy '*' -o /dev/null -w "HTTP=%{http_code}\n" \
  -X POST "http://100.103.240.78:18001/v1/embeddings" \
  -H 'Content-Type: application/json' \
  -d '{"input":"health probe","model":"/models/bge-m3"}'
# expect: HTTP=200
```

#### 5.3.2 Local backup embedding and primary/standby switching

A local TEI instance is kept as a **standby**, but it carries `profiles: ["local-embed"]` in
compose, so it **does not start by default** — seeing **0** `tei-embedding` entries in
`docker compose config --services` is expected.

When you need it (primary is down, or you want to run locally):

```bash
# start the local standby (first run must fetch the model: ./.models/fetch_bge_m3.sh "$PWD/bge-m3")
docker compose --profile local-embed up -d tei-embedding
```

> The host port is controlled by `TEI_EMBEDDING_PORT` (default `18002`). The amd64 image runs
> emulated on arm64, and **model load + warm up takes roughly 40–180s in practice** — `/health`
> being unreachable during that window is normal, don't declare failure too early.

Use `tools/embedding_failover.sh` to switch between primary and standby (it rewrites `base_url`
in RAGFlow's MySQL; **verified to take effect immediately, no RAGFlow restart required**):

| Command | Effect |
|---|---|
| `bash tools/embedding_failover.sh status` | Show which one is active + probe both |
| `bash tools/embedding_failover.sh check` | Probe only, primary and standby, **changes nothing** |
| `bash tools/embedding_failover.sh auto` | Decide automatically: fall back to standby if primary is down, back to primary if standby is down (300s cooldown against flapping) |
| `bash tools/embedding_failover.sh switch-back` | primary → standby (starts the local TEI for you) |
| `bash tools/embedding_failover.sh switch-main` | standby → primary (back to the remote host) |

> This script depends on the RAGFlow MySQL container `filebay-knowledge-trial-mysql-1`, which
> belongs to the "legacy bound stack" described below — **if that stack isn't running, the script
> can't read the current target and `status` will report "read failed"**.

#### 5.3.3 ⚠️ The knowledge backend is NOT yet independently deployed (no sugar-coating)

**This is the most important item in this section: RAGFlow is currently not an independent
stack. That has not been achieved physically yet.**

The actual situation:

- The `127.0.0.1:19380` entry point is served by an **nginx proxy** (`ekos-ragflow-proxy`);
- its **backend is still the `ragflow-cpu` container inside the legacy bound stack (FileBay
  stack)**, together with its MySQL / Elasticsearch / Redis / MinIO, all still under
  `filebay-knowledge-trial-*`;
- in other words: **RAGFlow is not part of this project's `docker compose` orchestration.**
  There is **no** RAGFlow among the 8 services this repo starts — `docker compose up -d`
  **will not** bring it up.

Current state:

| Capability | Status |
|---|---|
| Dual mock datasets via Wren, metadata collection, requirement intake, attachments, semantic analysis | ✅ self-contained in this stack; `docker compose up -d` is enough |
| Knowledge retrieval `knowledge_search` / `knowledge_citation_list` | ⚠️ **depends on the external stack** — it must be started separately or these tools are unavailable |

To make knowledge retrieval work on a new machine, **you must first bring up that legacy bound
stack (FileBay stack) on the host**. Running only this repo's `docker compose up -d` is
**not enough**.

> This is tracked internally as tech debt P1-1. The gateway side is implemented with **no
> fallback branch**: when RAGFlow is unavailable it reports the error honestly and **never
> silently degrades into fake results** — a deliberate choice, since an honest error beats
> plausible-looking wrong data for business users.

### 5.4 Data Preparation Before First Run (required for business users)

**On a fresh machine both the metadata tables and the knowledge base are empty.** You must
collect / ingest first, otherwise:

- semantic analysis gets no slot candidates at all (it doesn't know which tables or fields exist);
- knowledge citations are always empty (nothing has been ingested to retrieve);
- the UI loads and you can submit a requirement, but **every result comes back empty** — easily
  misdiagnosed as "the system is broken".

The repo ships **4 ready-made knowledge documents** under `knowledge/` (metric definitions, data
security policy, table structure & granularity notes, requirement intake procedure), so ingesting
them takes one command.

**Step 1: collect metadata** (introspect both mock databases into the metadata dictionary)

```bash
# collect from dataset A + B, and seed the business glossary along the way
python tools/collect_metadata.py --dataset A --dataset B --seed-glossary
```

> `--dataset` is repeatable (`A` / `B`, the two mock databases). `--seed-glossary` seeds the
> business glossary — recommended, otherwise glossary Q&A hit rate suffers. A non-zero exit code
> means the consistency self-check failed; the output lists the physically missing fields.
> Default `--engine native` (via `information_schema`, no extra dependencies); add
> `--engine schemacrawler` to cross-check with the SchemaCrawler backend (requires local docker).

**Step 2: ingest the knowledge base** (upload `knowledge/*.md` into RAGFlow and trigger parsing)

```bash
# ingest every .md under knowledge/ and wait for parsing to finish
python tools/ingest_knowledge.py

# inspect current ingestion/parse status without uploading (for troubleshooting)
python tools/ingest_knowledge.py --status
```

> Preconditions: the legacy bound stack from 5.3.3 must be up, and `KNOWLEDGE_API_KEY` /
> `KNOWLEDGE_DATASET_ID` must be **filled in** in `.env` (otherwise the script exits with
> "未配置 KNOWLEDGE_API_KEY"). Parsing is asynchronous; the script waits up to 180s by default
> (`--wait` to change it). Note this script runs on the **host**, so `host.docker.internal` in
> `.env` is rewritten back to `127.0.0.1`.

After both steps, `/healthz` and semantic analysis have complete data. Skipping them still
leaves the rest of the stack usable — only the knowledge-related capabilities are empty.

### 5.7 Interfaces

| Item | URL | Notes |
|---|---|---|
| **MCP Endpoint** | `http://127.0.0.1:18080/mcp` | streamable-http (FastMCP 4.x; protocol version negotiated per client) |
| **Health Check** | `http://127.0.0.1:18080/healthz` | Per-dataset/component status; if `degraded`, inspect the `components` field |
| **Mock Dataset A (E-commerce)** | `127.0.0.1:9000`(MCP) / `15432`(PG) | 6 models / 29 fields / 5 relationships |
| **Mock Dataset B (Retail Member)** | `127.0.0.1:9002`(MCP) / `15433`(PG) | 8 models / 47 fields / 10 relationships |
| **Gateway Metadata DB** | `127.0.0.1:15434`(PG) | user=assistant / requirements, events, metadata glossary |
| **Attachment Storage** | `127.0.0.1:19000`(S3) / `19001`(Console) | MinIO, bucket `askoda-attachments` |
| **Knowledge Base (optional)** | `http://127.0.0.1:19380/api/v1` | RAGFlow v0.26.4 IR API; gateway acts only as a client |

Gateway host port is `18080` (not `8080`) because port `8080` is reserved for the local demo server `demo/demo-server.py`.

### 5.8 Troubleshooting FAQ

| Symptom | Root Cause | Fix |
|---|---|---|
| First call after `assistant-postgres` boot fails on missing tables | `init_schema` runs idempotently at gateway startup; PG not ready yet | `docker compose restart gateway`; health check will flip `degraded` back to `ok` |
| Wren MCP returns `503` / `connection refused` | Wren Engine cold-start takes 30–60s on first boot | Wait 60s and retry, or `docker compose logs wren-mcp-a` |
| Knowledge API reports `host.docker.internal` unreachable | RAGFlow not running on host or different network | Set actual reachable `KNOWLEDGE_API_URL` in `.env`, or leave blank — `degraded_sources` will report honestly |
| Volume `wren-pgdata` says `external volume not found` | Fresh host, no legacy volume from single-stack era | Change `WREN_PG_VOLUME=askoda_wren-pgdata` in `.env` or drop `external: true` to let compose manage it |
| `tools/*_selftest.py` raises ModuleNotFoundError | Some self-tests import gateway internals | Pure offline self-tests (`gates` / `rules` / `masking` / `knowledge` / `evidence` / `semantics`) run directly on the host; those that genuinely need internals: `docker cp` into container + run with `PYTHONPATH=/app` per the script header |
| Knowledge retrieval reports `未配置知识库 API Key` | `KNOWLEDGE_API_KEY` is empty in `.env`. After `cp .env.example .env` this key is **empty by default**, and `knowledge.py` in the gateway hard-fails on it | Put a real `KNOWLEDGE_API_KEY` (obtained from the RAGFlow side) in `.env`, then `docker compose up -d gateway` to recreate the container so the env var takes effect; `tools/ingest_knowledge.py` reads the same key |
| Knowledge retrieval reports `EmbeddingError` (code=100) | The remote embedding service `100.103.240.78:18001` is unreachable. RAGFlow doesn't cache query vectors and calls the embedding service per retrieval, so an unreachable address kills all retrieval | Follow 5.3.1 to confirm Tailscale is logged in; `curl` the probe above expecting HTTP 200; if it fails run `bash tools/embedding_failover.sh auto` to fall back to the local standby (requires the legacy bound stack from 5.3.3) |
| Frontend 404 / static assets (`/assets/*.js`) fail to load | `bff/static/` is missing or stale. If you changed `web/src/` without rebuilding, the page references hash filenames that don't exist | First confirm there are files under `bff/static/assets/`; then run `cd web && npm install && npm run build` to regenerate, followed by `docker compose restart bff` |
| Wren MCP not ready on cold start, first call fails | Wren Engine needs 30–60s to boot, and the gateway's `depends_on` only waits for `service_started` (neither service has a healthcheck, so readiness can't be detected) | `docker compose restart gateway` to make the gateway re-handshake; or wait a minute and retry, and watch `docker compose logs wren-mcp-a` for progress |

---

### 5.9 MCP Capability Map (41 Tools · 9 Domains)

<a id="mcp-tools"></a>

The gateway exposes **41 tools** over **one** streamable-http endpoint, grouped into nine business domains. **Tool names and parameters are governed by the `@mcp.tool` registrations in `gateway/app.py`**; the tables below match `tools/list` one-to-one (automated check: `tools/mcp_acceptance_check.py`). Full return-field contracts and edge conditions live in the project documentation workspace (MCP Tool Contract & Registration Notes).

| # | Domain | Count | Tools |
|---|---|---|---|
| 1 | Health & datasets | 4 | `gateway_health` `datasets` `knowledge_health` `schema_version` |
| 2 | Deterministic planning & Wren passthrough | 5 | `plan` `wren_manifest` `wren_dry_run` `wren_query` `ask` |
| 3 | Requirement intake | 5 | `demand_create` `demand_get` `demand_list` `demand_summarize` `demand_set_status` |
| 4 | Knowledge retrieval | 2 | `knowledge_search` `knowledge_documents` |
| 5 | Metadata glossary | 3 | `metadata_lookup` `metadata_glossary` `metadata_collect` |
| 6 | Attachments | 3 | `attachment_put` `attachment_list` `attachment_url` |
| 7 | Semantic analysis & clarification loop | 7 | `analysis_first_round` `analysis_rounds` `analysis_get` `analysis_evidence` `confirmation_generate` `confirmation_list` `confirmation_answer` |
| 8 | Structured requirement & context pack | 5 | `schema_scan` `requirement_structured` `requirement_get` `schema_candidates` `sql_context_pack` |
| 9 | Generate · Gate · Execute | 7 | `sql_review` `sql_plan` `sql_generate` `sql_execute_readonly` `sql_run_get` `sql_run_list` `sql_run_replay` |

Total **4+5+5+2+3+3+7+5+7 = 41**.

> Convention: parameters are shown as `name: type = default`; ★ marks required. `dataset` defaults to `B` for most tools (raw dataset A is used to prove zero-code swap). All inputs/outputs are JSON.

#### Domain 1 · Health & datasets (4)

| Tool | Parameters | Returns (key fields) | Use |
|---|---|---|---|
| `gateway_health` | — | `{status, service, version, datasets[], components{meta_db, attachments, knowledge}}` | Global liveness; run this first |
| `datasets` | — | `{datasets:[{key, label, models, fields, relationships}]}` | Confirm both datasets are registered |
| `knowledge_health` | — | `{ok, visible dataset, parsed docs}` | Knowledge-base availability |
| `schema_version` | `dataset="B"` | `{schema_version, tables, fields}` | Latest schema-snapshot version |

#### Domain 2 · Deterministic planning & Wren passthrough (5)

| Tool | Parameters | Returns (key fields) | Use |
|---|---|---|---|
| `plan` | ★`nl`, `dataset="B"` | `{blocked, intent, sql, objects, reason, steps}` | Deterministic NL→SQL (closed world; refuse on miss) |
| `wren_manifest` | `dataset="B"` | `{models[], relationships[]}` | Read the MDL manifest |
| `wren_dry_run` | ★`sql`, `dataset="B"` | `{ok, message, plan?}` | Semantic closed-set dry run (gate L4) |
| `wren_query` | ★`sql`, `dataset="B"` | `{ok, row_count, columns, data, dtypes}` | Read-only execute (read-only gate → dry_run) |
| `ask` | ★`nl`, `dataset="B"` | `{blocked, …, row_count, result}` | One-shot question: plan → gate → read-only run |

> `plan` / `ask` use the **deterministic planner only** — no semantic analysis, no clarification loop (that is `analysis_first_round`'s job).

#### Domain 3 · Requirement intake (5)

| Tool | Parameters | Returns (key fields) | Use |
|---|---|---|---|
| `demand_create` | ★`title` ★`business_context` ★`description` ★`expected_output` ★`contact`, `time_range=None`, `expected_finish_at=None`, `attachments=None`, `actor=""` | Requirement record (**masked by default**); on validation failure → `{ok:false, rejected:true, reason}` | Submission portal |
| `demand_get` | ★`demand_id`, `reveal=false`, `actor=""` | Record + event trace; `reveal=true` returns raw text and writes a `reveal_original` audit entry | Lookup |
| `demand_list` | `status=None`, `limit=20`, `offset=0` | Requirement list (filter by status) | Board |
| `demand_summarize` | `demand_id=None` | With ID = one record; without = global board | Admin |
| `demand_set_status` | ★`demand_id` ★`status`, `note=None`, `actor=""` | State transitions — append-only, never overwritten | Rollback loop |

Status values: `待分析 / 分析中 / 待业务确认 / 待补充修改 / 待审核通过 / 已通过 / 已退回` (pending analysis / analysing / awaiting business confirmation / awaiting revision / awaiting approval / approved / returned).

#### Domain 4 · Knowledge retrieval (2)

| Tool | Parameters | Returns (key fields) | Use |
|---|---|---|---|
| `knowledge_search` | ★`question`, `top_k=5`, `threshold=0.1`, `vector_weight=0.7`, `demand_id=""`, `round_no=0` | Cited hits: `document_name` / `chunk_id` / `positions`; passing `demand_id` logs citations into `knowledge_citations` | Evidence |
| `knowledge_documents` | `limit=50` | Indexed documents (name / parse state / chunk count) | Knowledge stock |

> Two measured caveats: `threshold` **must be > 0** (0 is falsy and silently falls back to 0.2); `vector_weight` defaults to 0.7 (upstream 0.3 over-weights keywords and misses long Chinese questions).

#### Domain 5 · Metadata glossary (3)

| Tool | Parameters | Returns (key fields) | Use |
|---|---|---|---|
| `metadata_lookup` | `table=None`, `dataset=None`, `keyword=None`, `include_hidden=false` | Table CN name / granularity / columns; `include_hidden=true` surfaces unmodeled columns marked "AI-invisible" | Data dictionary |
| `metadata_glossary` | `term=None`, `keyword=None` | Metric-caliber glossary (source tagged `mdl` / `manual`) | Data dictionary |
| `metadata_collect` | `dataset="B"`, `engine="native"` | Collect physical schema into the dictionary + consistency self-check | Onboarding |

#### Domain 6 · Attachments (3)

| Tool | Parameters | Returns (key fields) | Use |
|---|---|---|---|
| `attachment_put` | ★`filename` ★`content_base64`, `demand_id=None` | `{ok, attachment}`; text-like files (csv/txt/md/json) are PII-scanned on upload, hits reported in `risk` | Attach file |
| `attachment_list` | `demand_id=None` | `{ok, prefix, objects[]}` | List attachments |
| `attachment_url` | ★`object_key`, `expires_seconds=3600` | `{ok, url}` pre-signed download URL | Direct download |

#### Domain 7 · Semantic analysis & clarification loop (7)

| Tool | Parameters | Returns (key fields) | Use |
|---|---|---|---|
| `analysis_first_round` | ★`demand_id`, `dataset="B"`, `actor=""` | 6 slots / `evidence_chain` / `rule_check` (R1–R7) / `questions` / `dropped_questions` / `conflicts` / `degraded_sources` / `round_no`. **Rounds are append-only** | Run a round |
| `analysis_rounds` | ★`demand_id` | Metadata for all rounds | List rounds |
| `analysis_get` | ★`demand_id`, `round_no=None` | Full result of a round (latest by default) | Read verdict |
| `analysis_evidence` | ★`demand_id`, `round_no=None`, `level=None` | Evidence chain, filterable by P1–P9 | Read evidence |
| `confirmation_generate` | ★`demand_id`, `dataset="B"`, `actor=""` | **Refresh pending questions only**; no new round | Re-ask |
| `confirmation_list` | ★`demand_id`, `include_history=false` | Q&A; `include_history=true` replays all versions | Read Q&A |
| `confirmation_answer` | ★`demand_id` ★`question_id` ★`answer`, `choice=None`, `actor=""` | Answer; **never overwrites history** — inserts `version+1` with `supersedes` | Business answer |

> Don't mix the two audiences: `slots` is the **analyst view** (contains physical table/column names), `questions` is the **business view** (already passed the tech-word gate).

#### Domain 8 · Structured requirement & context pack (5)

| Tool | Parameters | Returns (key fields) | Use |
|---|---|---|---|
| `schema_scan` | `dataset="B"`, `persist=true` | Snapshot + stable `schema_version` (two scans of the same DB must match) | Onboarding |
| `requirement_structured` | ★`demand_id`, `dataset="B"` | Structured requirement object + `contract_ok` / `contract_errors`; append-only versioning | Compose |
| `requirement_get` | ★`demand_id`, `version=None` | Fetch a version (latest by default) | Fetch |
| `schema_candidates` | ★`demand_id`, `dataset="B"` | Subject table / join paths / time-field candidates; produced **only inside the MDL closed set**, returns `miss_reason` on miss | Prep |
| `sql_context_pack` | ★`demand_id`, `dataset="B"` | Context pack + provenance triple `schema_version` / `requirement_version` / `pack_version` | Prep |

#### Domain 9 · Generate · Gate · Execute (7)

| Tool | Parameters | Returns (key fields) | Use |
|---|---|---|---|
| `sql_review` | ★`sql`, `dataset="B"` | 5-layer gate review `{status, layers[]}`: L1 syntax / L2 static rules (tiered blocking) / L3 read-only / L4 semantic / L5 result assertion | Gate |
| `sql_plan` | ★`demand_id`, `dataset="B"` | Plan draft (**no SQL body**); returns candidate set marked "manual review" when no unique solution | Stage 1 |
| `sql_generate` | ★`demand_id`, `dataset="B"`, `candidate_sql=None` | SQL draft; falls back to the deterministic planner when `candidate_sql` is empty; `hold` when candidates diverge too much | Stage 2 |
| `sql_execute_readonly` | ★`demand_id`, `dataset="B"`, `sql=None`, `actor=""` | Runs only if all 5 gate layers pass; writes one `sql_runs` row per run (fully replayable) | Execute |
| `sql_run_get` | ★`demand_id` | All run records for one requirement | Delivery |
| `sql_run_list` | `demand_id=""`, `dataset=""`, `limit=20` | Cross-requirement run list (time-desc, filterable) | Audit |
| `sql_run_replay` | ★`demand_id`, `version=0` | Reconstruct input → pack_version → SQL → review → result for a given version | Audit replay |

> **L2 tiered blocking**: 4 hard-error rules block on hit — `JOIN_WITHOUT_CONDITION` / `ONE_TO_MANY_UNHANDLED` / `UNMAPPED_OBJECT_REF` / `ENUM_VALUE_INVALID`; the other 7 rules warn only.
>
> **Don't conflate the two rulesets**: the L2 above is the **SQL static ruleset** (`gateway/rules.py`, 11 rules over the SQL AST); `analysis_first_round`'s `rule_check` (R1–R7, Domain 7) is the **requirement-semantics ruleset** (`gateway/semantics.py`, over the request text). They are separate layers that evolve independently.

#### Canonical end-to-end flow (8-step chain)

```
demand_create
 → analysis_first_round         # 6 slots + evidence chain + R1–R7
   → confirmation_list          # anything to ask the business?
     → confirmation_answer      # business answers (multi-round OK)
   → requirement_structured     # compose structured requirement + contract check
     → sql_context_pack         # assemble context pack (with provenance triple)
       → schema_candidates      # subject table / joins / time field
       → sql_plan               # plan draft (no SQL)
       → sql_generate           # SQL draft (deterministic fallback if candidate_sql empty)
         → sql_review           # 5-layer gate (L2/L3/L4 may block here)
           → sql_execute_readonly   # runs only if all layers pass + trace
             → sql_run_get / sql_run_replay
```

`knowledge_search(question, demand_id=…)` can be interleaved at any step to log citations.

#### Minimal invocation (verify from a terminal)

```bash
# 1) Liveness: status=ok, both datasets ok
curl -s http://127.0.0.1:18080/healthz | python3 -m json.tool

# 2) Handshake + list tools (should return 41 tools)
SID=$(curl -sD- -o/dev/null -X POST http://127.0.0.1:18080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}' \
  | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}' | tr -d '\r')
curl -s -X POST http://127.0.0.1:18080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' -o /dev/null
curl -s -X POST http://127.0.0.1:18080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | grep -o '"name":"[a-z_]*"' | sort
```

> Agent clients (Doubao / WorkBuddy / Codex …) don't need raw HTTP: register `http://127.0.0.1:18080/mcp` as an MCP server and call tools by name.

---

### 5.10 MCP Operations & Version-Upgrade Guide

<a id="mcp-ops"></a>

#### 5.10.1 Pre/post-integration self-check (3 steps)

1. `curl -s http://127.0.0.1:18080/healthz | python3 -m json.tool` → `status` should be `ok` (the knowledge base is reported separately; if down it degrades honestly without taking the gateway down);
2. Client `tools/list` → should be **41**;
3. Smoke test: call `datasets` and confirm both datasets (A / B) are registered.

#### 5.10.2 Common operations

```bash
docker compose ps                 # check status (wren-mcp-a/b and assistant-minio have no healthcheck — plain "Up" is normal, see 5.2)
docker compose up -d --build      # rebuild after changing gateway/
python3 gateway/healthcheck.py --base http://127.0.0.1:18080 --deep   # dual liveness (HTTP + MCP, also calls gateway_health for real)
python3 tools/mcp_acceptance_check.py                                 # verify all 41 tools one by one
```

> ⚠️ Container code is flattened to `/app`, but `tools/` is not in the build context — `docker cp` before running in-container scripts. HTTP-based scripts **must run on the host**; inside the container they get connection refused.

#### 5.10.3 Version identifiers (the table people mix up during upgrades)

| Identifier | Where it appears | Meaning | When it changes |
|---|---|---|---|
| `GATEWAY_VERSION` (currently `0.3.0`) | `gateway_health.version` / startup log | **Gateway product version** | Every milestone |
| `serverInfo.version` (currently `4.0.10`) | MCP `initialize` handshake | **FastMCP library version** (`FastMCP(name)` passes no version, so the library default is used) | When `fastmcp` is upgraded |
| `rules_version` | `sql_review` / `sql_runs` | Content hash of the L2 ruleset | When `gateway/rules.py` changes |
| `schema_version` | `schema_scan` / context pack | sha256 (first 8) of the sorted "table.column:type" list | When DB schema changes |
| `requirement_version` | `requirement_*` | Structured-requirement version (append-only) | Every composition |
| `pack_version` | `sql_context_pack` | sha256 (first 8) of schema_version + requirement_version + ruleset version | When any of the above changes |
| Image tag `askoda-gateway:0.3.0` | `docker-compose.yml` | **Kept in sync with `GATEWAY_VERSION`** | Every milestone |

> Key reminder: the `4.0.10` reported in the MCP handshake is the **FastMCP library version, not the gateway version**. Use `gateway_health.version` (currently `0.3.0`) whenever you talk about the gateway version.

#### 5.10.4 Upgrade checklist (mandatory when the tool surface changes)

After editing `gateway/app.py` (adding/removing `@mcp.tool`), do the following in order:

1. **Update docs in the same batch**: the per-tool contract and domain counts in the project documentation workspace (MCP Tool Contract & Registration Notes) → the domain-count and per-domain tables in this README (both languages);
2. **Rebuild the image**: `BUILDX_CONFIG="$PWD/.buildx" docker compose build gateway && docker compose up -d`;
3. **Verify the tool surface**: the `tools/list` count and list must match the docs (`python3 tools/mcp_acceptance_check.py`);
4. **Run regressions**: `python3 tools/gate_all.py` (G0–G3 four-layer gate) to confirm no baseline regression;
5. **Sync the image tag** and `.env` (if new environment variables are involved).

> Knock-on red flags (tool-surface changes touch these assertions/lists — scan them all when writing the change ticket): the **count and list assertions** in `tools/mcp_acceptance_check.py`, the contract doc's domain-count table, and the domain-count tables in both READMEs.

---

### 5.11 Manual MCP Verification

To manually verify all 41 tools at the lowest cost, follow the steps in the project documentation workspace (MCP Manual Verification Plan). Fastest path: run `tools/mcp_acceptance_check.py` once for a machine-level verdict, then walk the 8-step chain in 5.5 with **a single** requirement record (covering ~30 tools), and finally fill in the dependency-free probes and negative checks.

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

### 6.2 Unified Gate (G0–G3)

```bash
# One command runs all four layers, producing gate-report.json / gate-report.md
# (both are run artifacts, not committed)
python3 tools/gate_all.py

# Daily quick run: static + unit layers only (no DB writes, no live chain)
python3 tools/gate_all.py --layers G0,G1

# Final acceptance: every red item blocks (ignores the known-red baseline table)
python3 tools/gate_all.py --strict
```

| Layer | Content | Writes DB |
|---|---|---|
| **G0** | Static self-check (syntax compile + dependency baseline) | No |
| **G1** | Unit self-test (`*_selftest.py`) | No |
| **G2** | Independent re-check (`*_check.py`) | No |
| **G3** | Live chain (liveness + E2E) | **Yes** — run with care |

> The gate **auto-discovers** scripts by naming convention: `*_selftest.py` → G1, `*_check.py` → G2, `*_verify.py` → G3. Exit code is the verdict: 0 all green, 1 any red.

### 6.3 End-to-End Drill

```bash
python3 poc-eval/poc_e2e_gateway.py
# Produces poc-eval/e2e-gateway-report.json (machine-readable, run artifact, not committed)
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

| Milestone | Deliverables | Acceptance | Result (real measured values) |
|---|---|---|---|
| **M1** Gateway Skeleton | 7 MCP tools + dual Wren dataset + deterministic ask fallback | Accepted | 7/7 ✅ |
| **M2** Requirement Intake | 14 tools: demand CRUD / masking / metadata / attachments / knowledge | Accepted | 22/22 ✅ |
| **M3** Semantics + Clarification Loop | Evidence P1–P9 + Rules R1–R7 + 7 tools | Accepted | 109/109 ✅ |
| **M4** 2-Stage SQL Generation | plan() → generate() + closed-set self-check + pack_version hash | Accepted | 51/0 ✅ (dataset A) |
| **M5** 5-Layer Gate + Pure Rule Base | sqlglot + R1–R7 canonicalisation + read-only + dry-plan + result assert | Accepted | 62/0 ✅ (dataset B) |
| **M6-1** Rule Version Fingerprints | `rules_version` embeds `source_sha8` of each `evaluate` | Accepted | ✅ |
| **M6-2** Fallback Matrix F1–F5 | Pure classification + F2 criterion fixed to (empty `chosen_table`) | Accepted | ✅ |
| **M6-3** Robustness (timeout/retry/concurrency) | 3-tier timeout config + READ_TOOLS 3× backoff + unique run_id | Accepted | 18/18 ✅ |
| **M6-4** Audit Trace + Replay | actor pass-through + knowledge_citations + dataset-backed list filter + 5-segment replay | Accepted | Section B 16/16 ✅ |
| **M6-5** M6 Final Acceptance | 9-item M6 acceptance sheet (enumerable rules / positive-negative cases / 5 fallback classes / 20 consecutive runs / 4 trace classes / replay / zero regression / 0-code dataset swap) | Accepted | 9/9 ✅ |

> The values above are **actual measurements** recorded at each milestone's acceptance; the original acceptance sheets and per-assertion evidence are archived in the project documentation workspace.
> The one-off acceptance scripts (`m2_verify` / `m5_verify` / `audit_verify` etc.) are development process artifacts and **are not distributed with this repository** — they were bound to the container internals of their time and hold no reproduction value for external users. Currently reproducible verification entry points are in chapter 6 (unified gate + offline self-tests + tool-surface check).

### 7.2 POC Question Bank Acceptance

| Bank | Positive Precision | Refusal Coverage | POC-3 Trap Intercept Rate |
|---|---|---|---|
| POC-1 (30 items) | 20/20 = 100% | 10/10 = 100% | — |
| POC-3 (10 items) | — | — | 10/10 = 100% |

### 7.3 Known Tech Debt (Non-Blocking)

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
| HC-07 | All 9 trace fields mandatory, no omitted rows | `tools/gate_all.py` G3 live-chain assertions |
| HC-08 | `actor` passed through into `sql_runs.actor`; never defaulted | `tools/gate_all.py` G3 trace assertions |
| HC-09 | On filter error, NEVER silently degrade to unfiltered list | `tools/gate_all.py` G3 dataset 3-state comparison |

### 8.3 3-Step Acceptance For New Features

1. **Write the self-cert script first**, drop it at `tools/<feature>_selftest.py` (auto-discovered as G1) or `<feature>_check.py` (auto-discovered as G2), all assertions, no deps on new code;
2. **Then implement**, first run `ast.parse` sweep;
3. **Finally run `python3 tools/gate_all.py --strict`** — all four gate layers green.

---

## 9. Roadmap

### Short-Term (v0.4 · End of Phase 2 → Phase 3)

- [x] **M6 · Rules + Robustness + Trace/Replay** (M6-0…M6-5): rule expansion (1:N / time calibration / load-date ban / DISTINCT misuse) + fallback matrix F1–F5 + timeout/retry/concurrency + audit replay; **M6-5 final acceptance 9/9**. Includes the POC-3 rule hardening (tiered L2 blocking + enum-value rule): trap intercept 70% → 100%, zero false positives
- [ ] **M7 · MCP Tool-Surface Completion + End-to-End Wiring** (Gate 2): finalise the 41-tool contract + agent orchestrates the full chain from a single business request + minimal frontend loop
- [ ] **M8 · Five-Menu Workbench + Interaction** (customer-facing design): Semantic Layer·MDL Dictionary / Knowledge Stock / Data-Source Onboarding / Requirement Analysis·SQL Generation / SQL Integration·Delivery
- [ ] **M9 · Real Business-System Integration + Real-Scenario Acceptance**

### Mid-Term (v0.5 · Planning)

- [ ] Semantic layer MDL online editor + PR-style change approval flow
- [ ] First-pass auto-rewrite after gate block (governance hint `how_to_fix` → patch SQL)
- [ ] Dataset registration API (currently code-registry based via `gateway/registry.py`)
- [ ] FastMCP 5.x upgrade + official `create_proxy` return (drop handwritten Wren client)
- [ ] **Ontology-native query (graph query / Cypher) pilot**: an ontology-native path for the Action layer, alongside SQL (not implemented today)

### Long-Term (v1.0 · Concept)

- [ ] Multi-tenancy + dataset-level fine-grained RBAC
- [ ] **Standard ontology stack (RDF / OWL / SKOS / SWRL …) + reasoner**: evolve the Logic layer from MDL into a reasonable ontology
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
