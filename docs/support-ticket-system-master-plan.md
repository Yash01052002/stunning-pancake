# Master Plan: Customer Support Ticket System with Auto-Triage

## 1. Overview

A support ticket platform where incoming customer requests (email, web form,
chat, API) are captured, automatically classified/prioritized/routed
("auto-triage"), and resolved by the right agent or bot with SLA tracking and
reporting.

**Goals**
- Reduce time-to-first-response via automatic classification and routing.
- Cut manual triage effort for support leads.
- Give agents full context (customer history, similar past tickets) at
  ticket-open time.
- Provide measurable SLA/CSAT reporting for management.

**Non-goals (initial release)**
- Full omnichannel voice/telephony support.
- Multi-language auto-response generation (may be a later phase).

---

## 2. High-Level Architecture

```
Channels (Email, Web Form, Widget, API, Slack)
        │
        ▼
  Ingestion Layer (webhooks / IMAP poller / API gateway)
        │
        ▼
   Ticket Service  ──►  Ticket DB (Postgres)
        │
        ▼
  Auto-Triage Engine
   ├─ Rule Engine (regex/keyword, business rules)
   ├─ Classifier (category, intent, sentiment)
   ├─ Priority Scorer (SLA risk, keywords, customer tier)
   └─ Router (team/agent assignment, load balancing)
        │
        ▼
   Notification Service (email/Slack/webhooks)
        │
        ▼
  Agent Console (Web UI) ── Admin/Reporting Dashboard
        │
        ▼
   Audit/Event Log (for analytics + retraining)
```

**Suggested stack** (adjust to team preference):
- Backend: Node.js/TypeScript (NestJS) or Python (FastAPI)
- DB: PostgreSQL (primary), Redis (queues/cache), S3-compatible storage (attachments)
- Queue: SQS/RabbitMQ/BullMQ for async triage jobs
- Classification: start with rule-based, evolve to an LLM-based classifier (e.g., Claude) via a triage microservice
- Frontend: React + TypeScript
- Infra: Docker, CI/CD (GitHub Actions), deployed to your existing cloud provider

---

## 3. Phased Delivery Plan

### Phase 0 — Discovery & Requirements (1 week)
- Define ticket sources (email inbox, web form, widget, API).
- Define categories/taxonomy (e.g., Billing, Bug, Feature Request, Account, Abuse).
- Define priority levels (P0–P3) and SLA targets per level/customer tier.
- Define team/queue structure (who owns what category).
- Draft data model and API contract (see §4, §5).
- **Exit criteria:** signed-off requirements doc, taxonomy list, SLA matrix.

### Phase 1 — Core Ticketing Foundation (2–3 weeks)
- Ticket CRUD service + Postgres schema (tickets, comments, attachments, customers, agents, teams).
- Ingestion: web form + REST API create-ticket endpoint.
- Basic agent console: list/filter/view/reply/close tickets.
- Manual assignment + status workflow (New → Open → Pending → Resolved → Closed).
- Auth/roles (Admin, Agent, Customer).
- Event log table (append-only) for every state transition.
- **Exit criteria:** agents can manually manage the full ticket lifecycle end-to-end.

### Phase 2 — Auto-Triage Engine v1 (Rule-Based) (2 weeks)
- Rule engine: keyword/regex matching → category assignment.
- Priority scoring: static rules (e.g., "refund", "down", "security" → P0/P1; customer tier weighting).
- Round-robin / load-based routing to teams based on category + priority.
- Async job pipeline: ticket created → enqueue triage job → apply rules → update ticket → notify.
- Fallback: unmatched tickets go to a default "Triage" queue for human review.
- Metrics: track auto-triage accuracy (agent overrides = signal).
- **Exit criteria:** >70% of tickets auto-categorized and routed without agent correction.

### Phase 3 — Auto-Triage Engine v2 (ML/LLM-Based) (3–4 weeks)
- Replace/augment rules with an LLM-based classifier (intent, category, sentiment, urgency) using historical labeled tickets (from Phase 2 overrides as training/eval data).
- Sentiment & urgency detection (angry/frustrated customer → priority bump).
- Duplicate/similar-ticket detection (vector search over past tickets, e.g., pgvector/embeddings) to suggest existing solutions.
- Confidence scoring: low-confidence predictions route to human triage queue instead of auto-assigning.
- A/B evaluate v2 vs v1 rule engine on accuracy and time-to-resolution.
- **Exit criteria:** auto-triage accuracy ≥ 85%, measurable drop in time-to-first-response.

### Phase 4 — SLA, Notifications & Escalation (2 weeks)
- SLA timers per priority/tier; breach warnings (approaching/breached).
- Escalation rules: auto-reassign or notify manager on SLA breach or no response within X hours.
- Notification channels: email, Slack/Teams webhook, in-app.
- Customer-facing status updates (ticket received, in progress, resolved) via email.
- **Exit criteria:** SLA breaches are visible in real time and trigger automatic escalation.

### Phase 5 — Agent Productivity & Automation (2–3 weeks)
- Suggested replies (canned responses + LLM-drafted reply based on similar resolved tickets).
- Macros/bulk actions (merge tickets, bulk close, bulk reassign).
- Internal notes, @mentions, collision detection (two agents editing same ticket).
- Customer self-service: suggested KB articles at ticket-submission time (deflection).
- **Exit criteria:** measurable reduction in average handle time.

### Phase 6 — Reporting, Analytics & Admin (2 weeks)
- Dashboards: volume by category, SLA compliance, CSAT, agent workload, auto-triage accuracy trend.
- Admin UI to manage taxonomy, routing rules, SLA policies, teams without code changes.
- Exportable reports (CSV/API) for leadership.
- **Exit criteria:** stakeholders can self-serve reporting without engineering involvement.

### Phase 7 — Hardening, Scale & Compliance (ongoing / 2 weeks before GA)
- Load testing ingestion + triage pipeline.
- PII handling review, data retention policy, audit logging.
- Rate limiting/abuse protection on public-facing ticket creation endpoints.
- Backup/DR plan for ticket DB.
- Security review (auth, RBAC, attachment scanning, injection risks in triage prompts if using LLMs).
- **Exit criteria:** passes security review; documented runbook; on-call alerting in place.

---

## 4. Core Data Model (draft)

```
customers(id, email, name, tier, created_at)
agents(id, name, email, team_id, active)
teams(id, name)
tickets(
  id, customer_id, subject, body, channel,
  category, priority, status, assigned_agent_id, assigned_team_id,
  sla_due_at, confidence_score, triage_method, // 'rule' | 'ml' | 'manual'
  created_at, updated_at, resolved_at
)
ticket_events(id, ticket_id, type, actor, payload_json, created_at)
comments(id, ticket_id, author_id, author_type, body, is_internal, created_at)
attachments(id, ticket_id, comment_id, url, filename, size)
triage_rules(id, pattern, category, priority, team_id, active, priority_order)
```

## 5. Key API Endpoints (draft)

```
POST   /api/tickets                 create ticket (public/API)
GET    /api/tickets/:id             fetch ticket + timeline
GET    /api/tickets?filters         list/search
PATCH  /api/tickets/:id             update status/assignment/priority
POST   /api/tickets/:id/comments    add reply/internal note
POST   /api/tickets/:id/triage      re-run triage (manual trigger)
GET    /api/admin/rules             manage triage rules
GET    /api/reports/sla             SLA compliance report
```

## 6. Auto-Triage Design Detail

1. **Signal extraction**: subject/body text, customer tier, past ticket history, channel, attachments present.
2. **Classification**: category + intent (rule engine in Phase 2 → LLM classifier in Phase 3), returns label + confidence.
3. **Priority scoring**: weighted function of category severity, keyword urgency signals, customer tier/SLA plan, sentiment score.
4. **Routing**: map (category, priority) → team; within team, load-based or round-robin assignment; skip to human queue if confidence < threshold.
5. **Feedback loop**: every agent override (recategorize/reprioritize/reassign) is logged and used to tune rules/retrain the classifier and to compute the auto-triage accuracy metric.

## 7. Success Metrics
- Auto-triage accuracy (% tickets not overridden by agents).
- Time-to-first-response (before/after).
- SLA compliance rate.
- Agent override rate trend (should decrease over time).
- CSAT score.

## 8. Risks & Mitigations
- **Misrouted P0 tickets** → confidence thresholds + fallback human queue + alerting on triage failures.
- **LLM cost/latency at scale** → cache embeddings, batch low-priority triage, keep rule engine as fast-path pre-filter.
- **Data privacy in ticket content sent to LLM** → redact/mask PII before sending to any external model provider; document data handling.
- **Bias/drift in classifier over time** → periodic re-evaluation against labeled sample set.

## 9. Rough Timeline
| Phase | Duration | Cumulative |
|---|---|---|
| 0. Discovery | 1 wk | 1 wk |
| 1. Core Ticketing | 2–3 wk | 3–4 wk |
| 2. Auto-Triage v1 (rules) | 2 wk | 5–6 wk |
| 3. Auto-Triage v2 (ML/LLM) | 3–4 wk | 8–10 wk |
| 4. SLA & Escalation | 2 wk | 10–12 wk |
| 5. Agent Productivity | 2–3 wk | 12–15 wk |
| 6. Reporting & Admin | 2 wk | 14–17 wk |
| 7. Hardening & GA | 2 wk | 16–19 wk |

*(~4 months to GA with a small team; phases 4–6 can partially overlap.)*
