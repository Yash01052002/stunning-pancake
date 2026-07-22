# Support Ticket System — Backend (Phase 1 + Phase 2)

FastAPI + PostgreSQL API implementing ticket CRUD, comments, event logging,
and JWT auth with role-based access (admin/agent/customer), plus a
rule-based auto-triage engine (Phase 2 v1: keyword rules → category/
priority/team, tier-based priority boost, load-based agent assignment,
fallback queue for unmatched tickets). ML/LLM-based triage (Phase 3) is not
yet implemented.

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
permissions, ticket lifecycle/event logging, comment visibility, and the
Phase 2 triage engine (rule matching, tier boost, fallback routing,
load-based assignment, override tracking, accuracy reporting).

## Data model

- `teams`, `users` (role: admin/agent/customer, `tier` on customers)
- `tickets` (status: new → open → pending → resolved → closed)
  - `triage_outcome` (`matched`/`unmatched`) — immutable, set once by the
    rule engine at creation time
  - `triage_method` (`rule`/`manual`) — current attribution; flips from
    `rule` to `manual` the first time staff edits category/priority/
    assignment, which is the override signal the accuracy report reads
  - `confidence_score` — `1.0` for a rule match; reserved for a real
    confidence value once Phase 3 adds ML/LLM classification
- `triage_rules` — ordered keyword rules (`keyword`, `category`, `priority`,
  `team_id`, `active`, `evaluation_order`); first active match wins
- `comments` (public replies vs. internal-only notes)
- `ticket_events` — append-only audit log; every create/update/comment/
  auto-triage action is recorded here

## Auto-triage (Phase 2 v1)

On ticket creation, a background task (its own DB session — the request's
session is already closed by the time background tasks run) evaluates
active `triage_rules` in `evaluation_order`, case-insensitive substring
match against `subject + body`. On the first match:

1. category/priority/team are set from the rule
2. premium-tier customers get a one-priority-level boost (capped at P0)
3. the least-loaded active agent on the assigned team (fewest tickets in
   `new`/`open`/`pending`) is auto-assigned
4. an `auto_triaged` ticket event records what happened

If no rule matches, the ticket is routed to a fallback team (named via
`FALLBACK_TRIAGE_TEAM_NAME`, default `"Triage"`) for human triage, or left
unassigned if that team doesn't exist.

**Why BackgroundTasks and not a queue (Celery/RQ + Redis):** the master
plan's "async job pipeline" is satisfied at v1 scale by FastAPI's built-in
BackgroundTasks — it decouples triage from the request/response without
adding new infra. Revisit this in Phase 7 (Hardening & Scale) if ticket
volume needs a real worker queue with retries/backpressure.

**Why substring keywords and not regex:** admin-supplied regex risks ReDoS;
plain keyword matching is sufficient for v1's "refund"/"down"/"security"
style rules and avoids that class of vulnerability entirely.

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
| POST | `/tickets/{id}/triage` | staff-only: manually re-run the rule engine (e.g. after adding a new rule) |
| POST | `/tickets/{id}/comments` | customers restricted to public replies on their own ticket |
| GET | `/tickets/{id}/comments` | internal notes hidden from customers |
| POST | `/triage-rules` | admin-only: create a rule |
| GET | `/triage-rules` | staff-only: list rules |
| PATCH | `/triage-rules/{id}` | admin-only: edit/enable/disable a rule |
| GET | `/reports/triage-accuracy` | staff-only: coverage/accuracy/override counts (see below) |

### Triage accuracy report

`GET /reports/triage-accuracy` returns:

- `total_tickets`, `matched`, `unmatched`, `overridden`
- `coverage_rate` = matched / total — how many tickets a rule fired on
- `accuracy_rate` = (matched − overridden) / matched — of the ones a rule
  fired on, how many staff left alone
- `auto_triage_success_rate` = (matched − overridden) / total — the master
  plan's Phase 2 exit criterion ("**>70%** of tickets auto-categorized and
  routed without agent correction")

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
