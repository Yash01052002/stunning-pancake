"""Populate the database with demo teams, users, and tickets for local development.

Run after migrations: python -m scripts.seed
"""
from app.database import SessionLocal
from app.models import CustomerTier, TicketChannel, TicketPriority, UserRole
from app import crud


def run() -> None:
    db = SessionLocal()
    try:
        billing = crud.create_team(db, "Billing")
        technical = crud.create_team(db, "Technical")

        admin = crud.create_user(
            db,
            email="admin@example.com",
            password="admin12345",
            full_name="Ada Min",
            role=UserRole.ADMIN,
        )
        agent = crud.create_user(
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

        ticket = crud.create_ticket(
            db,
            customer_id=customer.id,
            subject="Invoice shows wrong amount",
            body="My latest invoice charged me twice for the same plan.",
            channel=TicketChannel.WEB,
            actor=customer,
        )
        crud.update_ticket(
            db,
            ticket,
            {"category": "billing", "priority": TicketPriority.P2, "assigned_team_id": billing.id},
            actor=agent,
        )

        print("Seeded demo data:")
        print(f"  admin:    admin@example.com / admin12345")
        print(f"  agent:    agent@example.com / agent12345 (team: Technical)")
        print(f"  customer: customer@example.com / customer12345")
        print(f"  teams:    {billing.name}, {technical.name}")
        print(f"  ticket:   {ticket.id} ({ticket.subject})")
    finally:
        db.close()


if __name__ == "__main__":
    run()
