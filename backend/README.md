# Support Ticket System — Backend (Phases 1–7)

FastAPI + PostgreSQL API implementing ticket CRUD, comments, event logging,
JWT auth with role-based access (admin/agent/customer), a rule-based
auto-triage engine (Phase 2: keyword rules → category/priority/team,
tier-based priority boost, load-based agent assignment, fallback queue for
unmatched tickets), an LLM-based auto-triage engine (Phase 3: Claude
classifies category/sentiment/priority/confidence; low-confidence results
route to human review instead of auto-assigning), SLA timers with
escalation and notifications (Phase 4: per-(priority, tier) SLA policies,
first-response/resolution clocks, breach escalation, and customer/agent
notifications across in-app + email + Slack), agent-productivity tooling
(Phase 5: canned responses, LLM-drafted suggested replies, bulk
close/reassign, ticket merge, @mentions in internal notes, collision
detection, and customer self-service KB deflection), reporting, analytics &
admin (Phase 6: CSAT ratings, volume / SLA-compliance / CSAT / agent-workload
/ triage-trend dashboards, CSV exports, and admin taxonomy / team management),
and hardening (Phase 7: PII redaction + prompt-injection resistance before LLM
calls, per-IP rate limiting on public endpoints, and append-only audit
logging — plus an ops/security runbook at [`docs/runbook.md`](../docs/runbook.md)).
Which triage engine runs is a config default, overridable per-ticket for A/B
comparison.

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

Seed demo data (admin/agent/customer accounts, teams, triage rules, SLA
policies, and tickets showing matched / unmatched / escalated-breach
outcomes) once the stack is up:

```bash
docker compose exec api python -m scripts.seed
```

## Console walkthrough (no server needed)

To watch the whole system work end-to-end in your terminal — every phase,
driven through the real service/CRUD code on a throwaway in-memory SQLite DB,
no Postgres / server / API key required:

```bash
cd backend
python -m scripts.demo
```

It prints each phase's outcome (triage routing, SLA escalation, notifications,
merge/@mentions, reports, PII redaction, audit log). The LLM features fall back
to a clearly-labelled offline stub when `ANTHROPIC_API_KEY` isn't set, so the
routing logic stays visible. It's a guided tour, not production code.

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
override tracking, accuracy reporting), the LLM engine (a fake classifier is
injected via `monkeypatch` — no `ANTHROPIC_API_KEY` or network access
needed), and Phase 4 SLA/escalation/notifications (policy precedence, due-date
computation, first-response tracking, `sla_status` transitions, idempotent
escalation with priority bump + reassignment, and notification scoping), and
Phase 5 productivity (canned-response CRUD/filtering, suggested replies with a
fake reply drafter, KB deflection ranking, bulk close/reassign, ticket merge,
@mention notifications, and presence/collision detection), Phase 6
reporting (CSAT validation/permissions, each report's math, CSV export shape,
team rename, categories listing), and Phase 7 hardening (PII redaction,
prompt fencing, rate limiting, and audit logging). Email/Slack and the LLM
reply drafter are never actually invoked over the network in tests — they
no-op / are monkeypatched. Rate limiting is disabled by default in the suite
via an autouse fixture (it's process-global state); the dedicated rate-limit
tests enable it explicitly.

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
  - `first_response_due_at` / `resolution_due_at` — SLA deadlines, set from
    the matching policy when the ticket gets a priority
  - `first_responded_at` — stamped by the first public staff comment;
    `resolved_at` doubles as the resolution-clock stop
  - `escalated_at` — set once when an SLA breach escalation fires; makes
    escalation idempotent
  - `first_response_sla_status` / `resolution_sla_status` — **computed on
    read** (not stored): `on_track`/`at_risk`/`breached`/`met`/`breached_late`,
    so a breach is always visible without a background job keeping a column
    fresh
  - `merged_into_id` — set when the ticket is merged into another as a
    duplicate (the source is closed; points at the survivor)
  - `csat_rating` (1–5) / `csat_comment` / `csat_submitted_at` — customer
    satisfaction rating, submittable once the ticket is resolved/closed
- `triage_rules` — ordered keyword rules (`keyword`, `category`, `priority`,
  `team_id`, `active`, `evaluation_order`); first active match wins for the
  rule engine, and the table doubles as the category→team map the LLM
  engine uses to route a classified ticket (see below)
- `sla_policies` — first-response/resolution targets in minutes, keyed by
  `(priority, tier)`; a tier-specific policy beats a `tier=null` one for the
  same priority
- `notifications` — in-app notifications (always created); one row per
  recipient per event (`type` includes `mentioned` for @-mentions)
- `canned_responses` — reusable reply snippets; optional `category` scopes a
  snippet to matching tickets (null = offered on every ticket)
- `kb_articles` — knowledge-base articles for self-service deflection, matched
  by comma-separated `keywords`
- `ticket_presence` — one heartbeat row per (ticket, staff viewer) for
  collision detection; unique on `(ticket_id, user_id)`
- `comments` (public replies vs. internal-only notes)
- `ticket_events` — append-only *business* audit log; every create/update/
  comment/auto-triage/sla_escalated/merge/csat action is recorded here
- `audit_logs` — append-only *security* audit log (Phase 7): one row per
  state-changing HTTP request (actor / method / path / status / IP), written
  by middleware; stores no request bodies

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
without adding new infra. The swap point for a real worker queue with
retries/backpressure (Celery/RQ/Arq + Redis) is
`tickets._run_triage_in_background`; see `docs/runbook.md` → "Scale & load
testing" for when to make that move.

**A/B comparison between engines:** set `AUTO_TRIAGE_ENGINE=llm` to make it
the default, or leave it on `rule` and use `POST /tickets/{id}/triage?engine=llm`
per ticket to compare the two engines' output side by side without
changing the default for other traffic.

## SLA, escalation & notifications (Phase 4)

### SLA clocks

Each ticket has two independent clocks, set from the matching `sla_policies`
row once the ticket has a priority (during triage, or when staff sets
priority manually):

- **first response** — stopped by the first *public* staff comment
  (internal notes don't count); `first_responded_at`
- **resolution** — stopped by status → `resolved`; `resolved_at`

Policy lookup is `(priority, tier)` with tier as the more specific match: a
policy scoped to a tier wins over a `tier=null` policy for the same
priority, so you can set a general P1 target plus a tighter P1 target just
for premium customers.

Each clock's live state (`first_response_sla_status` /
`resolution_sla_status`) is **computed on every read**, not stored:
`on_track` → `at_risk` (within `SLA_AT_RISK_WINDOW_MINUTES`, default 30, of
the deadline) → `breached`, or `met` / `breached_late` once the clock stops.
This means a breach is always accurately visible in `GET /tickets/{id}`
without needing a background job just to keep a status column current.

### Escalation

Seeing a breach and *acting* on it are separate. `POST /sla/escalate`
(staff) scans open, not-yet-escalated tickets and, for each breach:

1. bumps priority one level (capped at P0)
2. reassigns to the least-loaded agent on the ticket's team
3. logs an `sla_escalated` ticket event and notifies the newly-assigned
   agent (in-app + email) and a Slack channel

It's **idempotent** via `escalated_at` — safe to run repeatedly. There's no
in-process scheduler: run it from an external cron/scheduler hitting the
endpoint, or `python -m scripts.run_sla_escalations` as a standalone job
(host crontab, k8s CronJob, etc.).

**Why an externally-triggered idempotent endpoint and not a background
asyncio loop:** with multiple API workers, each would run its own loop and
could double-escalate the same ticket. An idempotent check triggered
externally behaves identically under any number of workers, and keeps
scheduling infra out of the app process (revisit alongside the Phase 7
worker-queue decision).

### Notifications

Three channels, degrading gracefully:

- **in-app** (`notifications` table) — always written; the reliable channel
  with no external dependency. `GET /notifications` / `PATCH
  /notifications/{id}/read` are scoped to the current user.
- **email** — sent only if `SMTP_HOST` is configured; otherwise logged and
  skipped. Never blocks the ticket action that triggered it.
- **Slack** — sent only if `SLACK_WEBHOOK_URL` is configured; same no-op
  behavior otherwise.

Customers are notified on ticket received, in-progress (status → open), and
resolved. Agents are notified on SLA escalation.

## Agent productivity (Phase 5)

- **Canned responses** — reusable snippets (`/canned-responses`, admin CRUD /
  staff read). A snippet's optional `category` scopes it to matching tickets.
- **Suggested replies** — `GET /tickets/{id}/suggested-replies` returns
  category-relevant canned responses plus an optional LLM-drafted reply
  (`app/reply_drafter.py`) grounded in a few recent resolved tickets in the
  same category. Same graceful-degradation contract as the Phase 3
  classifier: no `ANTHROPIC_API_KEY` / any failure → `drafted_reply` is
  simply `null`, canned responses still return. `?draft=false` skips the LLM
  call entirely. The draft is always for an agent to review — never
  auto-sent.
- **Bulk actions & merge** — `POST /tickets/bulk/close`,
  `POST /tickets/bulk/reassign`, and `POST /tickets/{id}/merge` (marks the
  given sources duplicates of this ticket and closes them). Bulk ops reuse
  the single-ticket update path per row so SLA recompute + notifications stay
  consistent; unknown ids are skipped.
- **@mentions** — an `@email` in an **internal** note notifies that staff
  member (`mentioned` notification). Public replies never trigger mentions,
  and customers can't be mentioned (internal-collaboration only).
- **Collision detection** — `POST /tickets/{id}/presence` is a heartbeat that
  returns the other agents currently viewing the ticket (seen within
  `PRESENCE_WINDOW_SECONDS`); `GET` lists them without registering the caller.
- **Self-service deflection** — `POST /kb/suggest` (any authenticated user,
  incl. customers) returns KB articles whose keywords match a ticket draft,
  ranked by hit count — surfaced before a customer submits, to deflect.

## Reporting, analytics & admin (Phase 6)

- **CSAT** — `POST /tickets/{id}/csat` lets the ticket's own customer rate it
  1–5 (+ optional comment) once it's resolved/closed.
- **Dashboards** (all staff-only JSON, computed in `app/analytics.py`):
  - `GET /reports/volume` — totals + counts by category / status / priority
  - `GET /reports/sla-compliance` — met/breached/pending for both the
    first-response and resolution clocks, with compliance rates. Reuses the
    computed-on-read `*_sla_status` properties, so "breached" means exactly
    what it does everywhere else.
  - `GET /reports/csat` — response count, average, 1–5 distribution
  - `GET /reports/agent-workload` — per-agent open / resolved / total assigned
  - `GET /reports/triage-trend` — auto-triage success rate bucketed by day
    (the accuracy-trend chart)
  - `GET /reports/triage-accuracy` — the Phase 2 point-in-time metric (kept)
  - `volume`, `sla-compliance`, `csat`, `triage-trend` accept
    `created_from`/`created_to` ISO datetime filters.
- **CSV export** (leadership) — `GET /reports/export/tickets.csv` (raw ticket
  dump, date-filterable) and `GET /reports/export/volume.csv`
  (volume-by-category), returned as `text/csv` attachments.
- **Admin management** — routing rules (`/triage-rules`), SLA policies
  (`/sla-policies`), and teams are all editable via the API without code
  changes: `PATCH /teams/{id}` renames a team, and `GET /admin/categories`
  shows the configured taxonomy alongside categories actually in use on
  rules/tickets (drift detection).

## Hardening, scale & compliance (Phase 7)

Defense-in-depth around the LLM integration, public endpoints, and the audit
trail. The parts that are process/infra rather than code (retention, backups,
load testing, on-call) live in the ops/security runbook at
[`docs/runbook.md`](../docs/runbook.md); what's implemented in code:

- **PII redaction before LLM egress** (`app/pii.py`) — ticket subject/body is
  scrubbed of email / phone / card- & SSN-shaped numbers before it's sent to
  Anthropic by either the triage classifier (`app/llm_classifier.py`) or the
  reply drafter (`app/reply_drafter.py`). Regex-based, so it's defense-in-depth
  (false negatives possible), not a guarantee — extend the patterns per the PII
  classes a given deployment handles.
- **Prompt-injection resistance** — the reply drafter fences the ticket (and
  each similar-ticket context block) in labeled *untrusted* delimiters and the
  system prompt instructs the model to treat that content as data, never
  instructions. The classifier uses structured outputs, which constrains the
  response shape regardless of what the ticket text says.
- **Rate limiting** (`app/rate_limit.py`) — per-IP fixed-window limits on the
  abuse-prone public endpoints (`POST /tickets`, `/auth/register`,
  `/auth/login`); returns `429` with a `Retry-After` header once a bucket is
  spent. Tunable via `RATE_LIMIT_*` (see `.env.example`); off when
  `RATE_LIMIT_ENABLED=false`.
- **Audit logging** (`app/audit.py`) — a middleware records one append-only
  `audit_logs` row per state-changing request (POST/PATCH/PUT/DELETE): actor
  (decoded from the bearer token, or null on a failed auth), method, path,
  status code, and client IP. **No request bodies are stored**, so
  credentials/PII don't accumulate in the trail. `GET /admin/audit-logs`
  (admin-only) reads it back, newest first. Auditing is wrapped so it can never
  break the underlying request.

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
| PATCH | `/teams/{id}` | admin-only: rename a team |
| POST | `/tickets` | customer creates own; staff can create on behalf of a customer via `customer_id`. Auto-triage runs after the response is sent — the returned ticket reflects pre-triage state |
| GET | `/tickets` | filterable list; customers see only their own tickets |
| GET | `/tickets/{id}` | full detail incl. comments + event timeline |
| PATCH | `/tickets/{id}` | staff-only: status/category/priority/assignment; marks the ticket `triage_method=manual` |
| POST | `/tickets/{id}/csat` | customer-only (own, resolved/closed): submit a 1–5 satisfaction rating |
| POST | `/tickets/{id}/triage?engine=` | staff-only: manually re-run auto-triage; optional `engine=rule\|llm` overrides the configured default for this call |
| POST | `/tickets/{id}/comments` | customers restricted to public replies on their own ticket |
| GET | `/tickets/{id}/comments` | internal notes hidden from customers |
| POST | `/triage-rules` | admin-only: create a rule |
| GET | `/triage-rules` | staff-only: list rules |
| PATCH | `/triage-rules/{id}` | admin-only: edit/enable/disable a rule |
| GET | `/reports/triage-accuracy` | staff-only: coverage/accuracy/override counts (see below) |
| POST | `/sla-policies` | admin-only: create an SLA policy |
| GET | `/sla-policies` | staff-only: list policies |
| PATCH | `/sla-policies/{id}` | admin-only: edit minutes / enable / disable |
| POST | `/sla/escalate` | staff-only: run the idempotent breach-escalation sweep |
| GET | `/notifications` | current user's notifications, newest first |
| PATCH | `/notifications/{id}/read` | mark one of your own notifications read |
| POST | `/canned-responses` | admin-only: create a canned response |
| GET | `/canned-responses?category=` | staff-only: list (optionally category-filtered) |
| PATCH | `/canned-responses/{id}` | admin-only: edit/enable/disable |
| GET | `/tickets/{id}/suggested-replies?draft=` | staff-only: canned + LLM-drafted reply |
| POST | `/tickets/bulk/close` | staff-only: bulk close by id list |
| POST | `/tickets/bulk/reassign` | staff-only: bulk set agent and/or team |
| POST | `/tickets/{id}/merge` | staff-only: merge source tickets into this one |
| POST | `/tickets/{id}/presence` | staff-only: heartbeat; returns other active viewers |
| GET | `/tickets/{id}/presence` | staff-only: list active viewers (no self-register) |
| POST | `/kb-articles` | admin-only: create a KB article |
| GET | `/kb-articles` | staff-only: list KB articles |
| PATCH | `/kb-articles/{id}` | admin-only: edit/enable/disable |
| POST | `/kb/suggest` | any authenticated user: KB articles matching a ticket draft |
| GET | `/reports/volume` | staff-only: volume by category/status/priority (date-filterable) |
| GET | `/reports/sla-compliance` | staff-only: first-response + resolution met/breached/pending |
| GET | `/reports/csat` | staff-only: CSAT count/average/distribution |
| GET | `/reports/agent-workload` | staff-only: per-agent open/resolved/total |
| GET | `/reports/triage-trend` | staff-only: auto-triage success rate by day |
| GET | `/reports/export/tickets.csv` | staff-only: raw ticket dump as CSV |
| GET | `/reports/export/volume.csv` | staff-only: volume-by-category as CSV |
| GET | `/admin/categories` | staff-only: configured vs. in-use categories |
| GET | `/admin/audit-logs` | admin-only: security audit trail, newest first (optional `actor_id` filter) |

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
- **SLA "business hours" are not modeled** — due dates are computed as a
  flat wall-clock offset from ticket creation (`created_at + N minutes`),
  not against a business calendar with working hours/holidays/time zones. A
  real deployment usually wants business-hour-aware SLA clocks; that's a
  calendar layer on top of `sla.apply_sla_targets` and deliberately out of
  scope for this pass.
- **Escalation runs on demand, not on a built-in schedule** — there's no
  in-process scheduler (see the SLA section above for why). Wire
  `POST /sla/escalate` or `scripts/run_sla_escalations.py` to cron / a k8s
  CronJob in a real deployment.
- **Regex rules**: the master plan mentions "regex/keyword" rules; only
  keyword (substring) matching is implemented in v1 for the ReDoS reason
  above. If regex is needed later, validate/sandbox patterns (e.g. a
  complexity check or a timeout-bounded matcher) before accepting
  admin-supplied ones.
- **Taxonomy is config + free-form strings, not an editable Category table**
  — categories live in `TRIAGE_CATEGORIES_CSV` (what the LLM classifies into)
  and as free strings on rules/tickets. `GET /admin/categories` surfaces both
  and flags drift, but there's no DB-backed category CRUD; "managing
  taxonomy" today means editing the config + rules. A first-class `categories`
  table with FK'd rules/tickets is the clean next step if the taxonomy needs
  to be fully self-service-editable.
- **Reports aggregate in Python, not via materialized rollups** — the Phase 6
  dashboards query and count in-process (`app/analytics.py`), which is fine at
  this scale and keeps "breached" defined in exactly one place (the
  computed-on-read SLA properties). High ticket volume would want SQL
  `GROUP BY` / a pre-aggregated reporting table or a read replica; the
  analytics functions are the swap-in point. Report date filters are
  normalized to naive-UTC to match SQLite storage (see `analytics._naive_utc`).
- **Semantic similarity search is still keyword/category-based, not vector**
  — the master plan mentions "vector search over past tickets" for both
  duplicate detection and reply drafting. Two lightweight stand-ins are
  implemented: the reply drafter's "similar resolved tickets" is *recent
  resolved tickets in the same category* (`crud.find_similar_resolved_tickets`),
  and KB deflection is *comma-separated keyword substring matching*
  (`crud.suggest_kb_articles`). Both are sufficient at this scale and need no
  extra infra. True semantic similarity (embeddings + pgvector, typically
  paired with Voyage AI since Anthropic has no embeddings endpoint) is the
  next slice — the two `crud` functions are the natural swap-in points.
- **Ticket merge is one-way and shallow** — merging sets `merged_into_id` and
  closes the source; it does **not** move the source's comments/attachments
  onto the survivor or de-duplicate customers. That's fine for flagging
  duplicates but a fuller merge (re-parenting history) would build on the
  same endpoint.
- **The LLM features require `ANTHROPIC_API_KEY`** to do anything — both the
  Phase 3 triage classifier and the Phase 5 reply drafter. Without it (or on
  any network/API failure) each returns `None` and the caller degrades
  gracefully: triage routes to the fallback team like an unmatched rule, and
  `suggested-replies` returns `drafted_reply: null` while still returning
  canned responses. This is by design, not an error path that needs fixing.
  The test suite never calls the real API; it injects fakes via
  `monkeypatch.setattr(triage, "get_classifier", ...)` /
  `monkeypatch.setattr(tickets_router, "get_drafter", ...)`.
- **Rate limiting is per-process, not shared** — `app/rate_limit.py` is an
  in-memory fixed-window limiter, so with `W` workers the effective ceiling is
  `W × configured` and counters reset on restart. It's a first line of defense;
  production should front it with a shared limiter (Redis-backed, an API
  gateway, or a WAF/CDN rate limit on the public ingress) and keep the in-app
  limiter as a backstop. See `docs/runbook.md` → "Rate limiting & abuse
  protection".
- **PII redaction is best-effort** — `app/pii.py` masks common email / phone /
  card / SSN shapes via regex before any ticket text reaches the LLM provider,
  but regex has false negatives and doesn't cover names/addresses/free-form
  identifiers. Treat it as defense-in-depth and extend the patterns for the PII
  classes a given deployment actually handles; it is not a substitute for a
  full DLP review.
