# ClinicMind

A CRM for dental and skin clinic enquiries with an **AI memory layer**, **third-party integrations** and **MCP tools** for agents.
All data is synthetic; no real patient information is used.

Clinic front desks lose context across WhatsApp, email and calls ("afraid of needles", "budget 60k, now 80k", "only free in the evening").
ClinicMind keeps that context, keeps it correct when it changes, and gives it to AI agents.

## What it does

| Area | What is built |
|---|---|
| **Memory** | LLM fact extraction (validated JSON) -> embeddings in pgvector -> reconcile (ADD / SUPERSEDE / NOOP with history and audit trail) -> hybrid retrieval (vector + keyword + recency + importance) -> token-budgeted context builder |
| **Background jobs** | Postgres job queue (`SKIP LOCKED`), worker with retries, backoff, dead-letter and stale-job recovery |
| **Integrations** | Signed webhooks (HMAC, replay window, idempotency), WhatsApp Cloud API webhook, Gmail via OAuth2 + PKCE with incremental sync, Slack alerts, Google Docs |
| **Agent tools** | One tool layer used by a built-in function-calling agent and by an **MCP server**; external actions need human approval |
| **Evals** | 8 synthetic patients, 48 questions: full history vs summary vs memory, scored on accuracy, stale answers, tokens, latency, cost |
| **Reliability** | LLM retries, timeouts, fallback model, per-call token/latency/cost log, clear 503s |
| **UI** | Memory viewer (including superseded history and *why* each memory exists), ask box that shows what the AI was shown, jobs, assistant, eval charts |

See [docs/architecture.md](docs/architecture.md) for the diagram and design decisions, and [docs/SETUP.md](docs/SETUP.md) for Slack, Gmail, WhatsApp and MCP setup.

## Quick start (Windows PowerShell)

```powershell
git clone https://github.com/Manha27/clinicmind.git
cd clinicmind
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # fill in GEMINI_API_KEY and WEBHOOK_SECRET
docker compose up -d
alembic upgrade head
python -m app.seed
python -m scripts.seed_demo     # demo patients
uvicorn app.main:app            # terminal 1  ->  http://127.0.0.1:8000/ui/
python -m app.worker            # terminal 2  ->  turns messages into memories
```

## Tests

```powershell
docker exec -it clinicmind-db psql -U clinicmind -d clinicmind -c "CREATE DATABASE clinicmind_test;"
python -m pytest -q
```
The suite never calls a real LLM or a real third-party API: Gmail, Google OAuth, Docs, Slack and the model run against fakes.

## Evals

```powershell
python -m evals.run --leads 2 --strategies memory --sleep 2
```
Open `evals/output/report.html` (or the Evals tab). Results from a real run belong here once you have them.

## Layout

```
app/            API, memory layer, jobs, integrations, tools, agent, UI (static)
mcp_server/     MCP server (stdio)
evals/          dataset, scoring, strategies, runner, report
scripts/        demo seeding, signed webhook senders, benchmarks
migrations/     Alembic
tests/          pytest
docs/           architecture, setup
```
