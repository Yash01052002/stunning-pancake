from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notifications, sla
from app.models import (
    Comment,
    Notification,
    SLAPolicy,
    Team,
    Ticket,
    TicketEvent,
    TicketStatus,
    TriageMethod,
    TriageRule,
    User,
    UserRole,
)
from app.security import hash_password


def _now() -> datetime:
    return datetime.now(timezone.utc)


_TRIAGE_RELEVANT_FIELDS = {"category", "priority", "assigned_team_id", "assigned_agent_id"}


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
    notifications.notify_customer_ticket_received(db, ticket)
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

    if _TRIAGE_RELEVANT_FIELDS & changes.keys():
        # a human just touched category/priority/assignment — this ticket is
        # no longer purely rule-owned, which is the override signal the
        # Phase 2 auto-triage accuracy metric reads.
        ticket.triage_method = TriageMethod.MANUAL

    if "priority" in changes:
        # a human (re)set priority manually — recompute SLA due dates the
        # same way triage does, from whichever policy now matches
        sla.apply_sla_targets(db, ticket)

    if "status" in changes:
        notifications.notify_customer_status_change(db, ticket, updates["status"])

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

    is_first_response = (
        author.role in (UserRole.AGENT, UserRole.ADMIN)
        and not is_internal
        and ticket.first_responded_at is None
    )
    if is_first_response:
        ticket.first_responded_at = _now()

    log_event(
        db,
        ticket,
        "commented",
        author,
        {"comment_id": comment.id, "is_internal": is_internal, "first_response": is_first_response},
    )
    db.commit()
    db.refresh(comment)
    return comment


# ---- Triage rules ----


def create_triage_rule(db: Session, **fields) -> TriageRule:
    rule = TriageRule(**fields)
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


def list_triage_rules(db: Session) -> list[TriageRule]:
    return list(db.scalars(select(TriageRule).order_by(TriageRule.evaluation_order)))


def get_triage_rule(db: Session, rule_id: str) -> TriageRule | None:
    return db.get(TriageRule, rule_id)


def update_triage_rule(db: Session, rule: TriageRule, updates: dict) -> TriageRule:
    for field, value in updates.items():
        if value is not None:
            setattr(rule, field, value)
    db.commit()
    db.refresh(rule)
    return rule


# ---- SLA policies ----


def create_sla_policy(db: Session, **fields) -> SLAPolicy:
    policy = SLAPolicy(**fields)
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


def list_sla_policies(db: Session) -> list[SLAPolicy]:
    return list(db.scalars(select(SLAPolicy).order_by(SLAPolicy.priority)))


def get_sla_policy(db: Session, policy_id: str) -> SLAPolicy | None:
    return db.get(SLAPolicy, policy_id)


def update_sla_policy(db: Session, policy: SLAPolicy, updates: dict) -> SLAPolicy:
    for field, value in updates.items():
        if value is not None:
            setattr(policy, field, value)
    db.commit()
    db.refresh(policy)
    return policy


# ---- Notifications ----


def list_notifications(db: Session, user_id: str) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
        )
    )


def get_notification(db: Session, notification_id: str) -> Notification | None:
    return db.get(Notification, notification_id)


def mark_notification_read(db: Session, notification: Notification) -> Notification:
    if notification.read_at is None:
        notification.read_at = _now()
        db.commit()
        db.refresh(notification)
    return notification
