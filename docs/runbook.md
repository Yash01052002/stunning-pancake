# Operations & Security Runbook (Phase 7)

Hardening, scale, and compliance notes for the support-ticket backend. Some of
Phase 7 is implemented in code (PII redaction, rate limiting, audit logging);
the rest is operational policy that lives here because it's process/infra, not
application code. This is the "documented runbook" the master plan's Phase 7
exit criteria call for.

## What's implemented in code

| Area | Where | Notes |
|---|---|---|
| PII redaction before LLM calls | `app/pii.py`, wired into `app/llm_classifier.py` + `app/reply_drafter.py` | Masks emails / phones / card- & SSN-shaped numbers before any ticket text is sent to Anthropic. Defense-in-depth, not a guarantee (regex has false negatives). |
| Prompt-injection resistance | `app/reply_drafter.py` (fenced, labeled untrusted content + system-prompt instruction), `app/llm_classifier.py` (structured outputs) | Ticket text is treated as data, not instructions. Structured outputs constrain the classifier's response shape. |
| Rate limiting | `app/rate_limit.py` on `POST /tickets`, `/auth/register`, `/auth/login` | Per-process fixed window. **Not** a shared limiter — see Scale below. |
| Audit logging | `app/audit.py` middleware → `audit_logs` table, `GET /admin/audit-logs` | Every mutating request (who / what / status / IP), append-only, no request bodies stored. |
| RBAC | `app/deps.py` (`require_staff`, `require_admin`) + per-endpoint customer-ownership checks | Customers only see/act on their own tickets; internal notes hidden from customers; staff/admin splits on every mutating admin endpoint. |
| Password storage | `app/security.py` | bcrypt, input truncated to 72 bytes (bcrypt's limit) explicitly. |

## Data retention & PII policy

- **Ticket content may contain PII** (names, emails, phone numbers in free-text
  bodies). It is stored in the `tickets` table and, for auditing, referenced
  (never duplicated) by `ticket_events` and `notifications`.
- **PII is redacted before leaving for the LLM provider** (`app/pii.py`). No
  raw ticket body is sent to Anthropic; redaction is best-effort and should be
  reviewed/extended for the specific PII classes each deployment handles.
- **Audit logs deliberately store no request bodies** — only method, path,
  status, actor id, and client IP — so credentials/PII don't accumulate there.
- **Retention**: define a retention window per data class (e.g. resolved
  tickets after N months, audit logs after N years for compliance) and enforce
  it with a scheduled purge job. Not implemented here — add a
  `scripts/purge_old_data.py` cron alongside `scripts/run_sla_escalations.py`.
- **Right-to-erasure (GDPR/CCPA)**: deleting a customer must cascade or
  anonymize their tickets/comments/notifications. `audit_logs.actor_id` is a
  plain string (not an FK) specifically so audit history survives user
  deletion; scrub PII from ticket content rather than dropping audit rows.

## Rate limiting & abuse protection

- Implemented as a per-process in-memory fixed-window limiter. With `W` API
  workers the effective limit is `W × configured`, and counters reset on
  restart.
- **This is a first line of defense, not the whole story.** For production put
  a shared limiter in front: Redis-backed limiter, an API gateway, or a WAF /
  CDN rate limit on the public ingress. Keep the in-app limiter as a backstop.
- Tunables: `RATE_LIMIT_ENABLED`, `RATE_LIMIT_TICKET_CREATE_PER_MINUTE`,
  `RATE_LIMIT_AUTH_PER_MINUTE`.
- Public ticket creation still requires authentication; truly anonymous
  ingestion channels (email intake, public web form) would need CAPTCHA /
  email-verification / per-source throttling added at the ingestion layer.

## Scale & load testing

- **Triage pipeline**: auto-triage runs in a FastAPI BackgroundTask, decoupled
  from the request. At high volume replace this with a real worker queue
  (Celery/RQ/Arq + Redis, or SQS) — the swap point is
  `tickets._run_triage_in_background`. The LLM engine adds provider latency and
  cost per ticket; batch or fast-path low-priority tickets through the rule
  engine.
- **Reporting**: `app/analytics.py` aggregates in Python. Under large ticket
  volumes move to SQL `GROUP BY`, a materialized reporting table, or a read
  replica. The analytics functions are the single swap-in point.
- **Load testing approach**: drive `POST /tickets` (ingestion) and the report
  endpoints with Locust/k6 at target peak RPS; watch DB connection-pool
  saturation, background-task backlog, and p95 latency. Establish SLOs
  (e.g. p95 create < 200ms excluding triage) before GA.
- **DB connection pooling**: tune SQLAlchemy pool size to the worker count ×
  concurrency; add PgBouncer in front of Postgres for many workers.

## Backup & disaster recovery

- **Backups**: enable automated Postgres backups — managed provider PITR
  (point-in-time recovery) or `pg_dump` on a schedule to object storage, with
  backups encrypted at rest and retention matching the data policy.
- **Restore drills**: periodically restore a backup into a scratch environment
  and verify integrity — an untested backup is not a backup.
- **RPO/RTO**: define targets (e.g. RPO ≤ 5 min via PITR, RTO ≤ 1 hr) and size
  the backup cadence / standby strategy to meet them.
- **Migrations**: schema changes ship as Alembic migrations; run
  `alembic upgrade head` on deploy (the Docker image already does). Test
  migrations against a production-shaped dump before applying.

## Security review checklist

- [x] RBAC enforced on every mutating endpoint (staff/admin/customer-ownership).
- [x] Passwords hashed (bcrypt); no plaintext, no reversible storage.
- [x] Auth via signed tokens; secret from `JWT_SECRET_KEY` (rotate the default!).
- [x] PII masked before egress to the LLM provider.
- [x] LLM prompts treat ticket content as untrusted data (injection-resistant).
- [x] Mutating actions audited (append-only, no bodies).
- [x] Rate limiting on public/abuse-prone endpoints.
- [ ] **Set a strong `JWT_SECRET_KEY`** in every non-dev environment — the
      default is a placeholder and must be overridden.
- [ ] **JWT library**: this build implements HS256 signing with the stdlib
      (`hmac`/`hashlib`) because the sandbox's `cryptography` extension is
      broken; swap to a maintained JWT library (PyJWT) in a normal environment.
- [ ] **TLS everywhere** — terminate HTTPS at the ingress; never serve the API
      over plaintext.
- [ ] **Attachments**: not implemented yet; when added, scan uploads
      (type/size validation + AV/malware scan) and store outside the app.
- [ ] **Secrets management**: inject `ANTHROPIC_API_KEY`, SMTP creds, DB creds
      via a secrets manager, not committed `.env` files.
- [ ] **Dependency scanning / SAST** in CI before GA.

## On-call & alerting

- **Health check**: `GET /health` for liveness/readiness probes.
- **SLA breaches**: `scripts/run_sla_escalations.py` (or `POST /sla/escalate`)
  on a cron; alert on-call if the escalation job fails or a P0 breaches.
- **Error rate / latency**: export metrics (request rate, error rate, p95
  latency, background-task backlog) and alert on SLO violations.
- **Audit anomalies**: alert on spikes in 401/403 in `audit_logs` (credential
  stuffing, privilege-probing).
- **Provider failures**: the LLM classifier/drafter degrade gracefully (fall
  back to rules / omit the draft) — track the fallback rate so a silent
  provider outage is visible rather than just quietly degrading quality.
