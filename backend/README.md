# Support Ticket System — Backend (Phase 1 + Phase 2 + Phase 3)

FastAPI + PostgreSQL API implementing ticket CRUD, comments, event logging,
JWT auth with role-based access (admin/agent/customer), a rule-based
auto-triage engine (Phase 2: keyword rules → category/priority/team,
tier-based priority boost, load-based agent assignment, fallback queue for
unmatched tickets), and an LLM-based auto-triage engine (Phase 3: Claude
classifies category/sentiment/priority/confidence; low-confidence results
route to human review instead of auto-assigning). Which engine runs is a
config default, overridable per-ticket for A/B comparison.

## Stack

- FastAPI, SQLAlchemy 2.0, Alembic
- PostgreSQL (production/dev via Docker), SQLite supported for quick local runs
- JWT auth (HS256, stdlib-only implementation — see note below), bcrypt password hashing

## Run with Docker Compose (recommended)

From the repo root:

```bash
docker compose up --build
```

This starts Postgres and the API on `http://localhost:8000`. Migrations run
automatically on container start. API docs: `http://localhost:8000/docs`.

Seed demo data (admin/agent/customer accounts, teams, triage rules, and two
tickets showing a matched vs. unmatched auto-triage outcome) once the stack
is up:

```bash
docker compose exec api python -m scripts.seed
```

## Run locally without Docker

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # defaults to SQLite if you don't have Postgres running
alembic upgrade head
python -m scripts.seed          # optional demo data
uvicorn app.main:app --reload
```

To point at Postgres instead of the SQLite default, set `DATABASE_URL` in
`.env` (see `.env.example`).

## Tests

```bash
cd backend
python -m pytest
```

Tests run against an in-memory SQLite database (schema created directly from
the SQLAlchemy models, independent of Alembic) and cover auth, role
permissions, ticket lifecycle/event logging, comment visibility, the rule
engine (matching, tier boost, fallback routing, load-based assignment,
override tracking, accuracy reporting), and the LLM engine (a fake
classifier is injected via `monkeypatch` — no `ANTHROPIC_API_KEY` or network
access is needed to run the suite).

## Data model

- `teams`, `users` (role: admin/agent/customer, `tier` on customers)
- `tickets` (status: new → open → pending → resolved → closed)
  - `triage_outcome` (`matched`/`low_confidence`/`unmatched`) — immutable,
    set once by whichever engine ran at creation/retrigger time
  - `triage_method` (`rule`/`llm`/`manual`/null) — current attribution;
    flips to `manual` the first time staff edits category/priority/
    assignment, which is the override signal the accuracy report reads.
    Stays null for `low_confidence` tickets — nothing has claimed them yet.
  - `confidence_score` — `1.0` for a rule match; the LLM's self-reported
    0.0–1.0 confidence for an LLM classification
  - `sentiment` (`positive`/`neutral`/`negative`/`angry`) — LLM engine only;
    `angry` triggers the same one-level priority boost as premium tier, and
    the two stack
- `triage_rules` — ordered keyword rules (`keyword`, `category`, `priority`,
  `team_id`, `active`, `evaluation_order`); first active match wins for the
  rule engine, and the table doubles as the category→team map the LLM
  engine uses to route a classified ticket (see below)
- `comments` (public replies vs. internal-only notes)
- `ticket_events` — append-only audit log; every create/update/comment/
  auto-triage action is recorded here

## Auto-triage

On ticket creation, a background task (its own DB session — the request's
session is already closed by the time background tasks run) runs the
configured engine (`AUTO_TRIAGE_ENGINE`, default `rule`). Either engine can
also be invoked explicitly per ticket via `POST /tickets/{id}/triage?engine=`.

### Rule engine (Phase 2)

Evaluates active `triage_rules` in `evaluation_order`, case-insensitive
substring match against `subject + body`. On the first match:

1. category/priority/team are set from the rule
2. premium-tier customers get a one-priority-level boost (capped at P0)
3. the least-loaded active agent on the assigned team (fewest tickets in
   `new`/`open`/`pending`) is auto-assigned
4. an `auto_triaged` ticket event records what happened

**Why substring keywords and not regex:** admin-supplied regex risks ReDoS;
plain keyword matching is sufficient for "refund"/"down"/"security" style
rules and avoids that class of vulnerability entirely.

### LLM engine (Phase 3)

Sends the ticket's subject + body to Claude (`app/llm_classifier.py`) and
asks for a structured classification — category, sentiment, priority,
confidence, one-sentence rationale — using the Anthropic SDK's
`client.messages.parse(..., output_format=LLMClassification)`, which
validates the response against a Pydantic model instead of hand-parsing
free text. Model: `LLM_MODEL` (default `claude-sonnet-5`); thinking is
disabled and no sampling parameters are set, matching Anthropic's guidance
for fast, deterministic classification tasks.

1. category maps to a team by matching an active `triage_rules.category`
   (case-insensitive) — the same table the rule engine uses, so there's one
   category→team mapping to maintain, not two
2. priority gets boosted one level (capped at P0) for premium-tier
   customers **and** for `angry` sentiment — both can stack
3. if confidence is below `LLM_CONFIDENCE_THRESHOLD` (default `0.6`), the
   classification is still stored (useful context for whoever picks it up)
   but the ticket is routed to the fallback team **without** auto-assigning
   an agent or claiming `triage_method` — a human decides from there
4. if the classifier itself fails (no `ANTHROPIC_API_KEY`, network error,
   any exception), `classify()` returns `None` and the ticket is routed to
   the fallback team exactly like an unmatched rule — this is a normal,
   expected outcome, not a 500

### Shared fallback behavior

If neither engine produces a confident match, the ticket is routed to a
fallback team (named via `FALLBACK_TRIAGE_TEAM_NAME`, default `"Triage"`)
for human triage, or left unassigned if that team doesn't exist.

**Why BackgroundTasks and not a queue (Celery/RQ + Redis):** the master
plan's "async job pipeline" is satisfied at this scale by FastAPI's
built-in BackgroundTasks — it decouples triage from the request/response
without adding new infra. Revisit this in Phase 7 (Hardening & Scale) if
ticket volume needs a real worker queue with retries/backpressure.

**A/B comparison between engines:** set `AUTO_TRIAGE_ENGINE=llm` to make it
the default, or leave it on `rule` and use `POST /tickets/{id}/triage?engine=llm`
per ticket to compare the two engines' output side by side without
changing the default for other traffic.

## API surface

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register` | public signup, always creates a customer account |
| POST | `/auth/login` | returns JWT |
| GET | `/auth/me` | current user |
| POST | `/users` | admin-only, creates agent/admin/customer accounts |
| GET | `/users` | staff-only |
| POST | `/teams` | admin-only |
| GET | `/teams` | staff-only |
| POST | `/tickets` | customer creates own; staff can create on behalf of a customer via `customer_id`. Auto-triage runs after the response is sent — the returned ticket reflects pre-triage state |
| GET | `/tickets` | filterable list; customers see only their own tickets |
| GET | `/tickets/{id}` | full detail incl. comments + event timeline |
| PATCH | `/tickets/{id}` | staff-only: status/category/priority/assignment; marks the ticket `triage_method=manual` |
| POST | `/tickets/{id}/triage?engine=` | staff-only: manually re-run auto-triage; optional `engine=rule\|llm` overrides the configured default for this call |
| POST | `/tickets/{id}/comments` | customers restricted to public replies on their own ticket |
| GET | `/tickets/{id}/comments` | internal notes hidden from customers |
| POST | `/triage-rules` | admin-only: create a rule |
| GET | `/triage-rules` | staff-only: list rules |
| PATCH | `/triage-rules/{id}` | admin-only: edit/enable/disable a rule |
| GET | `/reports/triage-accuracy` | staff-only: coverage/accuracy/override counts (see below) |

### Triage accuracy report

`GET /reports/triage-accuracy` returns:

- `total_tickets`, `matched`, `unmatched`, `overridden`
- `coverage_rate` = matched / total — how many tickets either engine
  confidently classified (excludes `low_confidence` and `unmatched`)
- `accuracy_rate` = (matched − overridden) / matched — of the confidently
  classified ones, how many staff left alone
- `auto_triage_success_rate` = (matched − overridden) / total — the master
  plan's exit criterion ("**>70%** of tickets auto-categorized and routed
  without agent correction")

The report doesn't currently break results out by engine (`rule` vs. `llm`)
— it treats `matched` the same regardless of which engine produced it. Add
a `triage_method` group-by if comparing engines' accuracy separately
becomes necessary.

## Notes / deviations from the master plan

- **JWT implementation**: the sandbox's system `cryptography` package (used
  by both `python-jose` and `PyJWT`) has a broken Rust extension in this dev
  environment, so JWT signing/verification is implemented directly with
  `hmac`/`hashlib` (HS256) instead of a third-party JWT library. This is
  functionally equivalent for HS256 but should be revisited (swap back to a
  maintained JWT library) once running in a normal environment without that
  constraint.
- **Attachments** are in the master plan's data model but out of scope so
  far (no file storage integration yet); add in a later phase alongside
  proper storage (S3-compatible) and virus/type scanning.
- **`sla_due_at` and SLA/escalation logic** belong to Phase 4 and are
  intentionally not in this schema yet.
- **Regex rules**: the master plan mentions "regex/keyword" rules; only
  keyword (substring) matching is implemented in v1 for the ReDoS reason
  above. If regex is needed later, validate/sandbox patterns (e.g. a
  complexity check or a timeout-bounded matcher) before accepting
  admin-supplied ones.
- **Duplicate/similar-ticket detection** (Phase 3 master-plan item: "vector
  search over past tickets ... to suggest existing solutions") is **not
  implemented yet**. This pass focused on the classifier itself (category/
  sentiment/priority/confidence) and confidence-gated routing. Adding
  similarity search is a reasonable next slice — it doesn't need pgvector
  or a hosted embeddings API to start (Anthropic doesn't offer one; the
  usual pairing is Voyage AI); a lightweight local bag-of-words cosine
  similarity over recent tickets would work for v1 at this scale.
- **LLM engine requires `ANTHROPIC_API_KEY`** to actually classify anything.
  Without it (or on any network/API failure), `classify()` returns `None`
  and the ticket is routed to the fallback team exactly like an unmatched
  rule — this is by design (see "LLM engine" above), not an error path that
  needs fixing. The test suite never calls the real API; it injects a fake
  classifier via `monkeypatch.setattr(triage, "get_classifier", ...)`.
