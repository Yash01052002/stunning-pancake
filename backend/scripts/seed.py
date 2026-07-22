"""Populate the database with demo teams, users, triage rules, and tickets
for local development.

Run after migrations: python -m scripts.seed
"""
from app.database import SessionLocal
from app.models import CustomerTier, TicketChannel, TicketPriority, UserRole
from app import crud, triage


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

        # matched: hits the refund rule; customer is PREMIUM so priority is
        # boosted from P2 -> P1, and it's routed to the Billing team.
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
    finally:
        db.close()


if __name__ == "__main__":
    run()
