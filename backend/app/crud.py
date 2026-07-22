from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Comment,
    Team,
    Ticket,
    TicketEvent,
    TicketStatus,
    User,
)
from app.security import hash_password


def _now() -> datetime:
    return datetime.now(timezone.utc)


def log_event(
    db: Session, ticket: Ticket, event_type: str, actor: User | None, payload: dict
) -> TicketEvent:
    event = TicketEvent(
        ticket_id=ticket.id, type=event_type, actor_id=actor.id if actor else None, payload=payload
    )
    db.add(event)
    return event


# ---- Users ----


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email))


def create_user(db: Session, *, email: str, password: str, full_name: str, role, tier=None, team_id=None) -> User:
    user = User(
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        role=role,
        tier=tier,
        team_id=team_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ---- Teams ----


def create_team(db: Session, name: str) -> Team:
    team = Team(name=name)
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


def list_teams(db: Session) -> list[Team]:
    return list(db.scalars(select(Team)))


# ---- Tickets ----


def create_ticket(
    db: Session, *, customer_id: str, subject: str, body: str, channel, actor: User
) -> Ticket:
    ticket = Ticket(customer_id=customer_id, subject=subject, body=body, channel=channel)
    db.add(ticket)
    db.flush()  # assign ticket.id before logging the event
    log_event(
        db,
        ticket,
        "created",
        actor,
        {"subject": subject, "channel": channel.value if hasattr(channel, "value") else channel},
    )
    db.commit()
    db.refresh(ticket)
    return ticket


def get_ticket(db: Session, ticket_id: str) -> Ticket | None:
    return db.get(Ticket, ticket_id)


def list_tickets(
    db: Session,
    *,
    customer_id: str | None = None,
    status: TicketStatus | None = None,
    category: str | None = None,
    priority=None,
    assigned_team_id: str | None = None,
    assigned_agent_id: str | None = None,
) -> list[Ticket]:
    stmt = select(Ticket)
    if customer_id is not None:
        stmt = stmt.where(Ticket.customer_id == customer_id)
    if status is not None:
        stmt = stmt.where(Ticket.status == status)
    if category is not None:
        stmt = stmt.where(Ticket.category == category)
    if priority is not None:
        stmt = stmt.where(Ticket.priority == priority)
    if assigned_team_id is not None:
        stmt = stmt.where(Ticket.assigned_team_id == assigned_team_id)
    if assigned_agent_id is not None:
        stmt = stmt.where(Ticket.assigned_agent_id == assigned_agent_id)
    stmt = stmt.order_by(Ticket.created_at.desc())
    return list(db.scalars(stmt))


def update_ticket(db: Session, ticket: Ticket, updates: dict, actor: User) -> Ticket:
    changes = {}
    for field, new_value in updates.items():
        if new_value is None:
            continue
        old_value = getattr(ticket, field)
        if old_value == new_value:
            continue
        setattr(ticket, field, new_value)
        changes[field] = {
            "old": old_value.value if hasattr(old_value, "value") else old_value,
            "new": new_value.value if hasattr(new_value, "value") else new_value,
        }

    if "status" in changes and updates.get("status") == TicketStatus.RESOLVED:
        ticket.resolved_at = _now()

    if changes:
        log_event(db, ticket, "updated", actor, {"changes": changes})
        db.commit()
        db.refresh(ticket)
    return ticket


# ---- Comments ----


def add_comment(
    db: Session, ticket: Ticket, *, author: User, body: str, is_internal: bool
) -> Comment:
    comment = Comment(
        ticket_id=ticket.id, author_id=author.id, body=body, is_internal=is_internal
    )
    db.add(comment)
    db.flush()
    log_event(
        db,
        ticket,
        "commented",
        author,
        {"comment_id": comment.id, "is_internal": is_internal},
    )
    db.commit()
    db.refresh(comment)
    return comment
