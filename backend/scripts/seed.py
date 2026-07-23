"""Populate the database with demo teams, users, triage rules, SLA
policies, and tickets for local development.

Run after migrations: python -m scripts.seed
"""
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import CustomerTier, TicketChannel, TicketPriority, UserRole
from app import crud, sla, triage


def run() -> None:
    db = SessionLocal()
    try:
        billing = crud.create_team(db, "Billing")
        technical = crud.create_team(db, "Technical")
        # fallback queue for tickets no rule matches (see settings.fallback_triage_team_name)
        crud.create_team(db, "Triage")

        admin = crud.create_user(
            db,
            email="admin@example.com",
            password="admin12345",
            full_name="Ada Min",
            role=UserRole.ADMIN,
        )
        billing_agent = crud.create_user(
            db,
            email="billing-agent@example.com",
            password="agent12345",
            full_name="Billy Billing",
            role=UserRole.AGENT,
            team_id=billing.id,
        )
        tech_agent = crud.create_user(
            db,
            email="agent@example.com",
            password="agent12345",
            full_name="Alex Agent",
            role=UserRole.AGENT,
            team_id=technical.id,
        )
        customer = crud.create_user(
            db,
            email="customer@example.com",
            password="customer12345",
            full_name="Cara Customer",
            role=UserRole.CUSTOMER,
            tier=CustomerTier.PREMIUM,
        )

        crud.create_triage_rule(
            db,
            name="Outage",
            keyword="down",
            category="incident",
            priority=TicketPriority.P0,
            team_id=technical.id,
            evaluation_order=10,
        )
        crud.create_triage_rule(
            db,
            name="Security",
            keyword="security",
            category="security",
            priority=TicketPriority.P0,
            team_id=technical.id,
            evaluation_order=20,
        )
        crud.create_triage_rule(
            db,
            name="Refund request",
            keyword="refund",
            category="billing",
            priority=TicketPriority.P2,
            team_id=billing.id,
            evaluation_order=30,
        )

        # general SLA targets per priority, plus a tighter P1 policy just for
        # premium customers (tier-specific policies win over the general one
        # for the same priority — see sla.find_sla_policy)
        crud.create_sla_policy(
            db, priority=TicketPriority.P0, tier=None,
            first_response_minutes=15, resolution_minutes=240,
        )
        crud.create_sla_policy(
            db, priority=TicketPriority.P1, tier=None,
            first_response_minutes=60, resolution_minutes=1440,
        )
        crud.create_sla_policy(
            db, priority=TicketPriority.P1, tier=CustomerTier.PREMIUM,
            first_response_minutes=30, resolution_minutes=720,
        )
        crud.create_sla_policy(
            db, priority=TicketPriority.P2, tier=None,
            first_response_minutes=240, resolution_minutes=4320,
        )
        crud.create_sla_policy(
            db, priority=TicketPriority.P3, tier=None,
            first_response_minutes=1440, resolution_minutes=10080,
        )

        # matched: hits the refund rule; customer is PREMIUM so priority is
        # boosted from P2 -> P1, and it's routed to the Billing team. Gets
        # SLA due dates from the premium-tier P1 policy above.
        matched_ticket = crud.create_ticket(
            db,
            customer_id=customer.id,
            subject="Requesting a refund",
            body="I was charged twice for the same plan, please refund the duplicate charge.",
            channel=TicketChannel.WEB,
            actor=customer,
        )
        triage.run_auto_triage(db, matched_ticket)

        # unmatched: no rule matches, falls back to the Triage team for a human to categorize.
        unmatched_ticket = crud.create_ticket(
            db,
            customer_id=customer.id,
            subject="Question about my plan",
            body="Can you tell me what features are included in my current plan?",
            channel=TicketChannel.WEB,
            actor=customer,
        )
        triage.run_auto_triage(db, unmatched_ticket)

        # breached: same outage rule (P0), but its first-response deadline is
        # backdated into the past to demonstrate escalation without waiting
        # 15 real minutes. check_and_escalate_slas() below picks it up.
        breached_ticket = crud.create_ticket(
            db,
            customer_id=customer.id,
            subject="Production is down",
            body="Nothing is loading, this is urgent.",
            channel=TicketChannel.WEB,
            actor=customer,
        )
        triage.run_auto_triage(db, breached_ticket)
        breached_ticket.first_response_due_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.commit()

        escalation_report = sla.check_and_escalate_slas(db)

        # Phase 5: canned responses (one category-scoped, one general) + KB
        # articles for self-service deflection.
        crud.create_canned_response(
            db,
            title="Refund acknowledgement",
            body="Thanks for reaching out — I've confirmed the duplicate charge and "
            "issued a refund, which should appear in 3-5 business days.",
            category="billing",
        )
        crud.create_canned_response(
            db,
            title="Ask for more detail",
            body="Thanks for getting in touch! Could you share a bit more detail so I "
            "can help — for example any error message and when it started?",
            category=None,
        )
        crud.create_kb_article(
            db,
            title="How to reset your password",
            body="Go to Settings > Security > Reset password and follow the emailed link.",
            keywords="password,reset,login,sign in",
        )
        crud.create_kb_article(
            db,
            title="Understanding your invoice",
            body="Invoices are issued monthly; duplicate charges are auto-refunded within 5 days.",
            keywords="invoice,billing,charge,refund",
        )

        print("Seeded demo data:")
        print("  admin:         admin@example.com / admin12345")
        print("  billing agent: billing-agent@example.com / agent12345 (team: Billing)")
        print("  tech agent:    agent@example.com / agent12345 (team: Technical)")
        print("  customer:      customer@example.com / customer12345 (tier: premium)")
        print(f"  teams:         {billing.name}, {technical.name}, Triage")
        print(
            f"  matched ticket:   {matched_ticket.id} -> "
            f"category={matched_ticket.category} priority={matched_ticket.priority} "
            f"team={matched_ticket.assigned_team_id}"
        )
        print(
            f"  unmatched ticket: {unmatched_ticket.id} -> "
            f"team={unmatched_ticket.assigned_team_id} (fallback Triage queue)"
        )
        db.refresh(breached_ticket)
        print(
            f"  breached ticket:  {breached_ticket.id} -> "
            f"priority={breached_ticket.priority} escalated_at={breached_ticket.escalated_at} "
            f"(escalation run: {escalation_report['escalated_count']} ticket(s))"
        )
        print("  SLA policies:  P0/P1/P1-premium/P2/P3 seeded")
        print("  canned:        2 responses; KB: 2 articles (deflection)")
    finally:
        db.close()


if __name__ == "__main__":
    run()
