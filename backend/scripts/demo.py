"""Interactive console walkthrough of every phase of the support-ticket system.

Run it and watch the whole system work end-to-end in your terminal — no HTTP
server, no Postgres, no API key required:

    cd backend
    python -m scripts.demo

It spins up a throwaway in-memory SQLite database, builds the schema straight
from the SQLAlchemy models (same as the test suite — no migrations needed),
and drives each phase through the real service/CRUD code paths, printing what
happened at each step. The LLM features (Phase 3 classifier, Phase 5 reply
drafter) fall back to a clearly-labelled deterministic stub when no
`ANTHROPIC_API_KEY` is set, so the routing logic is still visible offline.

Nothing here is production code — it's a guided tour of the codebase.
"""
from __future__ import annotations

import os

# Use a private in-memory DB for the demo, decided before any app import so
# `app.config` / `app.database` pick it up. Never touches your real database.
os.environ["DATABASE_URL"] = "sqlite://"

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import crud, database, sla, triage  # noqa: E402
from app import llm_classifier  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.llm_classifier import LLMClassification  # noqa: E402
from app.models import (  # noqa: E402
    CustomerTier,
    TicketChannel,
    TicketPriority,
    TicketStatus,
    UserRole,
)


# --------------------------------------------------------------------------- #
# console helpers
# --------------------------------------------------------------------------- #

def banner(phase: str, title: str) -> None:
    line = "=" * 74
    print(f"\n{line}\n  {phase}  —  {title}\n{line}")


def step(msg: str) -> None:
    print(f"  • {msg}")


def detail(msg: str) -> None:
    print(f"      {msg}")


# --------------------------------------------------------------------------- #
# offline LLM stub — keeps Phase 3 / 5 visible without an API key
# --------------------------------------------------------------------------- #

class _StubClassifier:
    """Deterministic stand-in for the real Anthropic classifier so the demo
    shows the LLM engine's routing/boosting logic offline. Clearly labelled."""

    def classify(self, subject: str, body: str) -> LLMClassification | None:
        text = f"{subject} {body}".lower()
        if any(w in text for w in ("hack", "breach", "phish", "security")):
            return LLMClassification(
                category="security", sentiment="angry", priority="p0",
                confidence=0.94, rationale="Mentions a security compromise.",
            )
        if any(w in text for w in ("charge", "refund", "invoice", "billing")):
            return LLMClassification(
                category="billing", sentiment="negative", priority="p2",
                confidence=0.88, rationale="Billing dispute.",
            )
        # deliberately low confidence → exercises the human-review fallback path
        return LLMClassification(
            category="general", sentiment="neutral", priority="p3",
            confidence=0.35, rationale="No clear category.",
        )


class _StubDrafter:
    def draft(self, subject, body, similar):
        return (
            "Hi — thanks for reaching out. I've looked into this and we're on it; "
            "you'll have an update shortly. [stub draft — no ANTHROPIC_API_KEY set]"
        )


def _install_offline_stubs() -> bool:
    """Return True if we patched in stubs (no API key), False if a real key is
    present and the genuine Anthropic path will be used."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return False
    triage.get_classifier = lambda: _StubClassifier()  # type: ignore[assignment]
    llm_classifier.get_classifier = lambda: _StubClassifier()  # type: ignore[assignment]
    return True


# --------------------------------------------------------------------------- #
# setup: fresh schema on the throwaway DB
# --------------------------------------------------------------------------- #

def _bootstrap_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # point the app's SessionLocal at our in-memory engine so every service
    # call (which opens its own SessionLocal) lands on the same DB
    database.SessionLocal = TestingSession
    database.engine = engine
    return TestingSession()


# --------------------------------------------------------------------------- #
# the walkthrough
# --------------------------------------------------------------------------- #

def run() -> None:
    stubbed = _install_offline_stubs()
    db = _bootstrap_db()
    try:
        print("\nSupport-ticket system — console walkthrough")
        print("(throwaway in-memory SQLite; no server, no Postgres)")
        if stubbed:
            print("LLM: offline stub classifier/drafter (no ANTHROPIC_API_KEY).")
        else:
            print("LLM: live Anthropic API (ANTHROPIC_API_KEY detected).")

        # ---------------- Phase 1: foundation ----------------
        banner("Phase 1", "Foundation: teams, users, RBAC, tickets, comments")
        billing = crud.create_team(db, "Billing")
        technical = crud.create_team(db, "Technical")
        crud.create_team(db, settings.fallback_triage_team_name)  # "Triage"
        step(f"Created teams: Billing, Technical, {settings.fallback_triage_team_name}")

        admin = crud.create_user(
            db, email="admin@example.com", password="admin12345",
            full_name="Ada Min", role=UserRole.ADMIN,
        )
        billing_agent = crud.create_user(
            db, email="billing-agent@example.com", password="agent12345",
            full_name="Billy Billing", role=UserRole.AGENT, team_id=billing.id,
        )
        tech_agent = crud.create_user(
            db, email="tech-agent@example.com", password="agent12345",
            full_name="Tess Tech", role=UserRole.AGENT, team_id=technical.id,
        )
        customer = crud.create_user(
            db, email="customer@example.com", password="customer12345",
            full_name="Cara Customer", role=UserRole.CUSTOMER, tier=CustomerTier.PREMIUM,
        )
        step("Created admin / 2 agents / 1 premium customer (roles drive RBAC)")

        ticket = crud.create_ticket(
            db, customer_id=customer.id, subject="Hello",
            body="Just trying out support.", channel=TicketChannel.WEB, actor=customer,
        )
        crud.add_comment(
            db, ticket, author=customer, body="Any update?", is_internal=False
        )
        crud.add_comment(
            db, ticket, author=tech_agent,
            body="Looking into it now.", is_internal=False,
        )
        step(f"Opened ticket {ticket.id[:8]} with a public reply thread")
        detail(f"status={ticket.status.value}  first_responded_at set="
               f"{ticket.first_responded_at is not None}")

        # ---------------- Phase 2: rule-based triage ----------------
        banner("Phase 2", "Rule-based auto-triage: keyword → category/priority/team")
        crud.create_triage_rule(
            db, name="Outage", keyword="down", category="incident",
            priority=TicketPriority.P0, team_id=technical.id, evaluation_order=10,
        )
        crud.create_triage_rule(
            db, name="Refund", keyword="refund", category="billing",
            priority=TicketPriority.P2, team_id=billing.id, evaluation_order=30,
        )
        step("Seeded rules: 'down'→incident/P0/Technical, 'refund'→billing/P2/Billing")

        refund = crud.create_ticket(
            db, customer_id=customer.id, subject="Requesting a refund",
            body="I was charged twice, please refund the duplicate.",
            channel=TicketChannel.WEB, actor=customer,
        )
        triage.run_auto_triage(db, refund, engine="rule")
        db.refresh(refund)
        step(f"'refund' ticket auto-triaged by the rule engine")
        detail(f"category={refund.category}  priority={refund.priority.value} "
               f"(P2 boosted→P1 for premium tier)  "
               f"assigned_agent={'yes' if refund.assigned_agent_id else 'no'}")

        unmatched = crud.create_ticket(
            db, customer_id=customer.id, subject="General question",
            body="What plans do you offer?", channel=TicketChannel.WEB, actor=customer,
        )
        triage.run_auto_triage(db, unmatched, engine="rule")
        db.refresh(unmatched)
        step("Unmatched ticket routed to the human fallback queue (Triage)")
        detail(f"triage_outcome={unmatched.triage_outcome.value}")

        # ---------------- Phase 3: LLM triage ----------------
        banner("Phase 3", "LLM auto-triage: Claude classifies category/sentiment/priority")
        # the LLM engine maps its predicted category to a team via the same
        # triage_rules table the rule engine uses — add a 'security' route so a
        # classified security ticket lands on the Technical team
        crud.create_triage_rule(
            db, name="Security", keyword="__llm_security_route__", category="security",
            priority=TicketPriority.P0, team_id=technical.id, evaluation_order=20,
        )
        secincident = crud.create_ticket(
            db, customer_id=customer.id, subject="I think my account was hacked",
            body="There are logins I don't recognise and a security alert email.",
            channel=TicketChannel.WEB, actor=customer,
        )
        triage.run_auto_triage(db, secincident, engine="llm")
        db.refresh(secincident)
        step("Security ticket classified by the LLM engine"
             + (" (stub)" if stubbed else ""))
        detail(f"category={secincident.category}  sentiment="
               f"{secincident.sentiment.value if secincident.sentiment else None}  "
               f"priority={secincident.priority.value}  "
               f"confidence={secincident.confidence_score}")
        detail("angry sentiment + premium tier both boost priority (capped at P0)")

        lowconf = crud.create_ticket(
            db, customer_id=customer.id, subject="hmm",
            body="not sure how to describe this", channel=TicketChannel.WEB, actor=customer,
        )
        triage.run_auto_triage(db, lowconf, engine="llm")
        db.refresh(lowconf)
        step("Low-confidence classification → held for human review, not auto-assigned")
        detail(f"triage_outcome={lowconf.triage_outcome.value}  "
               f"confidence={lowconf.confidence_score} "
               f"(threshold={settings.llm_confidence_threshold})")

        # ---------------- Phase 4: SLA + escalation + notifications ----------------
        banner("Phase 4", "SLA clocks, breach escalation, notifications")
        crud.create_sla_policy(
            db, priority=TicketPriority.P0, tier=None,
            first_response_minutes=15, resolution_minutes=240,
        )
        crud.create_sla_policy(
            db, priority=TicketPriority.P1, tier=CustomerTier.PREMIUM,
            first_response_minutes=30, resolution_minutes=720,
        )
        step("SLA policies: P0 (15m/4h), premium-P1 (30m/12h)")
        # the refund ticket was triaged (P1) back in Phase 2 before any policy
        # existed; now that one does, applying targets sets its clocks
        sla.apply_sla_targets(db, refund)
        db.commit()
        db.refresh(refund)
        detail(f"refund ticket first_response_due_at set: "
               f"{refund.first_response_due_at is not None}  "
               f"status={refund.first_response_sla_status}")

        # backdate a P0 breach so escalation has something to act on right now
        from datetime import datetime, timedelta, timezone
        secincident.first_response_due_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()
        report = sla.check_and_escalate_slas(db)
        db.refresh(secincident)
        step(f"Ran the idempotent escalation sweep: "
             f"{report['escalated_count']} ticket(s) escalated")
        detail(f"escalated ticket now priority={secincident.priority.value}  "
               f"escalated_at set={secincident.escalated_at is not None}")
        agent_notifs = crud.list_notifications(db, secincident.assigned_agent_id or admin.id)
        detail(f"in-app notifications for the assigned agent: {len(agent_notifs)} "
               f"(email/Slack no-op when unconfigured)")

        # ---------------- Phase 5: agent productivity ----------------
        banner("Phase 5", "Canned replies, LLM drafts, bulk ops, merge, @mentions, presence")
        crud.create_canned_response(
            db, title="Refund ack",
            body="Confirmed the duplicate charge — refund issued, 3-5 business days.",
            category="billing",
        )
        canned = crud.list_canned_responses(db, category="billing")
        step(f"Canned responses for 'billing': {len(canned)} available")

        if stubbed:
            drafter = _StubDrafter()
            draft = drafter.draft(refund.subject, refund.body, [])
        else:
            from app.reply_drafter import get_drafter, SimilarTicket
            similar = [
                SimilarTicket(t.subject, t.body, resolution)
                for t, resolution in crud.find_similar_resolved_tickets(db, refund, limit=3)
            ]
            draft = get_drafter().draft(refund.subject, refund.body, similar)
        step("Suggested LLM reply draft (agent reviews before sending):")
        detail(draft)

        # a second refund-ish ticket so we can show bulk + merge
        dup = crud.create_ticket(
            db, customer_id=customer.id, subject="duplicate charge again",
            body="same refund issue", channel=TicketChannel.WEB, actor=customer,
        )
        merged_ids = crud.merge_tickets(db, refund, [dup.id], actor=billing_agent)
        step(f"Merged {len(merged_ids)} duplicate into ticket {refund.id[:8]}")
        db.refresh(dup)
        detail(f"source status={dup.status.value}  "
               f"merged_into set={dup.merged_into_id is not None}")

        crud.add_comment(
            db, refund, author=billing_agent,
            body=f"@{tech_agent.email} can you sanity-check the refund?", is_internal=True,
        )
        mention_notifs = crud.list_notifications(db, tech_agent.id)
        step(f"@mention in an internal note pinged the tech agent: "
             f"{len(mention_notifs)} notification(s)")

        crud.touch_presence(db, refund.id, billing_agent.id)
        viewers = crud.list_active_presence(
            db, refund.id, settings.presence_window_seconds, exclude_user_id=tech_agent.id
        )
        step(f"Collision detection: {len(viewers)} other agent(s) viewing the ticket")

        # ---------------- Phase 6: reporting & analytics ----------------
        banner("Phase 6", "CSAT, dashboards, CSV export, admin taxonomy")
        # resolve + rate the refund ticket so reports have signal
        crud.update_ticket(db, refund, {"status": TicketStatus.RESOLVED}, actor=billing_agent)
        crud.submit_csat(db, refund, rating=5, comment="Fast fix!", actor=customer)

        from app import analytics
        vol = analytics.volume_report(db)
        csat = analytics.csat_report(db)
        slac = analytics.sla_compliance_report(db)
        workload = analytics.agent_workload_report(db)
        step("Volume report:")
        detail(f"total={vol.total}  by_status={vol.by_status}")
        step("CSAT report:")
        detail(f"responses={csat.responses}  average={csat.average_rating}")
        step("SLA compliance (first response):")
        detail(f"met={slac.first_response_met}  breached={slac.first_response_breached}  "
               f"pending={slac.first_response_pending}")
        step("Agent workload:")
        for row in workload.agents[:3]:
            detail(f"{row.full_name}: assigned={row.total_assigned} "
                   f"open={row.open_tickets} resolved={row.resolved_tickets}")

        # ---------------- Phase 7: hardening ----------------
        banner("Phase 7", "PII redaction, prompt fencing, rate limiting, audit log")
        from app.pii import redact
        raw = "Contact me at jane.doe@example.com or 555-123-4567, card 4111 1111 1111 1111"
        step("PII redaction before any LLM egress:")
        detail(f"in : {raw}")
        detail(f"out: {redact(raw)}")

        from app.reply_drafter import _build_prompt
        prompt = _build_prompt("Refund for jane@x.com", "call 555-123-4567", [])
        fenced = "untrusted" in prompt.lower() and "jane@x.com" not in prompt
        step(f"Reply-drafter prompt fences + redacts untrusted ticket text: {fenced}")

        step("Rate limiting on public endpoints (per-IP fixed window):")
        detail(f"enabled={settings.rate_limit_enabled}  "
               f"ticket_create/min={settings.rate_limit_ticket_create_per_minute}  "
               f"auth/min={settings.rate_limit_auth_per_minute}")

        # audit log is written by HTTP middleware; here we show the read path
        from app.models import AuditLog
        db.add(AuditLog(
            actor_id=customer.id, method="POST", path="/tickets",
            status_code=201, client_ip="203.0.113.7",
        ))
        db.commit()
        logs = crud.list_audit_logs(db, limit=5)
        step(f"Security audit log (written by middleware in the live app): "
             f"{len(logs)} row(s)")
        if logs:
            e = logs[0]
            detail(f"{e.method} {e.path} → {e.status_code}  actor={e.actor_id[:8]}  "
                   f"ip={e.client_ip}  (no request body stored)")

        banner("Done", "All 7 phases exercised end-to-end")
        print("  Tour complete. Explore the API instead with:  uvicorn app.main:app --reload\n")
    finally:
        db.close()


if __name__ == "__main__":
    run()
