# Support Ticket System — Backend (Phase 1: Core Ticketing Foundation)

FastAPI + PostgreSQL API implementing ticket CRUD, comments, event logging,
and JWT auth with role-based access (admin/agent/customer). Auto-triage
(Phase 2+) is not yet implemented — tickets are categorized/prioritized/
assigned manually by staff via `PATCH /tickets/{id}`.

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

Seed demo data (admin/agent/customer accounts + a sample ticket) once the
stack is up:

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
permissions, ticket lifecycle/event logging, and comment visibility rules.

## Data model

- `teams`, `users` (role: admin/agent/customer)
- `tickets` (status: new → open → pending → resolved → closed)
- `comments` (public replies vs. internal-only notes)
- `ticket_events` — append-only audit log; every create/update/comment
  action is recorded here, which Phase 2's auto-triage accuracy metric will
  read from (agent overrides = signal)

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
| POST | `/tickets` | customer creates own; staff can create on behalf of a customer via `customer_id` |
| GET | `/tickets` | filterable list; customers see only their own tickets |
| GET | `/tickets/{id}` | full detail incl. comments + event timeline |
| PATCH | `/tickets/{id}` | staff-only: status/category/priority/assignment |
| POST | `/tickets/{id}/comments` | customers restricted to public replies on their own ticket |
| GET | `/tickets/{id}/comments` | internal notes hidden from customers |

## Notes / deviations from the master plan

- **JWT implementation**: the sandbox's system `cryptography` package (used
  by both `python-jose` and `PyJWT`) has a broken Rust extension in this dev
  environment, so JWT signing/verification is implemented directly with
  `hmac`/`hashlib` (HS256) instead of a third-party JWT library. This is
  functionally equivalent for HS256 but should be revisited (swap back to a
  maintained JWT library) once running in a normal environment without that
  constraint.
- **Attachments** are in the master plan's data model but out of scope for
  Phase 1 (no file storage integration yet); add in a later phase alongside
  proper storage (S3-compatible) and virus/type scanning.
- **Auto-triage fields** (`sla_due_at`, `confidence_score`, `triage_method`,
  `triage_rules` table) are intentionally not in this schema yet — they
  belong to Phases 2–4 and will be added via new migrations when those
  phases start, rather than speculatively now.
