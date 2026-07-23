from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from datetime import timedelta

from app import notifications, sla
from app.models import (
    CannedResponse,
    Comment,
    KBArticle,
    Notification,
    SLAPolicy,
    Team,
    Ticket,
    TicketEvent,
    TicketPresence,
    TicketStatus,
    TriageMethod,
    TriageRule,
    User,
    UserRole,
    as_aware_utc,
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


def get_team(db: Session, team_id: str) -> Team | None:
    return db.get(Team, team_id)


def rename_team(db: Session, team: Team, name: str) -> Team:
    team.name = name
    db.commit()
    db.refresh(team)
    return team


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


def submit_csat(db: Session, ticket: Ticket, *, rating: int, comment: str | None, actor: User) -> Ticket:
    """Records a customer satisfaction rating. Overwrites any prior rating
    (a customer can revise). Caller enforces the resolved/closed + ownership
    preconditions."""
    ticket.csat_rating = rating
    ticket.csat_comment = comment
    ticket.csat_submitted_at = _now()
    log_event(db, ticket, "csat_submitted", actor, {"rating": rating})
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

    # @mentions are an internal-collaboration feature — only parsed on internal
    # notes (a customer-visible reply shouldn't quietly ping staff)
    if is_internal:
        notifications.notify_mentions(db, ticket, author, body)

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


# ---- Canned responses (Phase 5) ----


def create_canned_response(db: Session, **fields) -> CannedResponse:
    canned = CannedResponse(**fields)
    db.add(canned)
    db.commit()
    db.refresh(canned)
    return canned


def list_canned_responses(db: Session, category: str | None = None) -> list[CannedResponse]:
    """Active responses whose category is null (applies to all) or matches the
    given category. With category=None, returns every active response."""
    stmt = select(CannedResponse).where(CannedResponse.active == True)  # noqa: E712
    if category is not None:
        stmt = stmt.where(
            (CannedResponse.category.is_(None)) | (CannedResponse.category == category)
        )
    return list(db.scalars(stmt.order_by(CannedResponse.title)))


def get_canned_response(db: Session, canned_id: str) -> CannedResponse | None:
    return db.get(CannedResponse, canned_id)


def update_canned_response(db: Session, canned: CannedResponse, updates: dict) -> CannedResponse:
    for field, value in updates.items():
        if value is not None:
            setattr(canned, field, value)
    db.commit()
    db.refresh(canned)
    return canned


def find_similar_resolved_tickets(
    db: Session, ticket: Ticket, limit: int
) -> list[tuple[Ticket, str]]:
    """Recent resolved/closed tickets in the same category (excluding this one),
    each paired with its last public agent reply as the 'resolution'. This is
    the lightweight keyword-free stand-in for vector similarity — see the
    Phase 5 deflection/similarity note in the README. Returns [] if the ticket
    has no category yet."""
    if ticket.category is None:
        return []
    rows = db.scalars(
        select(Ticket)
        .where(
            Ticket.category == ticket.category,
            Ticket.id != ticket.id,
            Ticket.status.in_((TicketStatus.RESOLVED, TicketStatus.CLOSED)),
        )
        .order_by(Ticket.resolved_at.desc().nullslast(), Ticket.updated_at.desc())
        .limit(limit)
    )
    results: list[tuple[Ticket, str]] = []
    for past in rows:
        resolution = next(
            (
                c.body
                for c in reversed(past.comments)
                if not c.is_internal and c.author and c.author.role != UserRole.CUSTOMER
            ),
            "",
        )
        results.append((past, resolution))
    return results


# ---- KB articles (Phase 5) ----


def create_kb_article(db: Session, **fields) -> KBArticle:
    article = KBArticle(**fields)
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


def list_kb_articles(db: Session) -> list[KBArticle]:
    return list(db.scalars(select(KBArticle).order_by(KBArticle.title)))


def get_kb_article(db: Session, article_id: str) -> KBArticle | None:
    return db.get(KBArticle, article_id)


def update_kb_article(db: Session, article: KBArticle, updates: dict) -> KBArticle:
    for field, value in updates.items():
        if value is not None:
            setattr(article, field, value)
    db.commit()
    db.refresh(article)
    return article


def suggest_kb_articles(db: Session, text: str, limit: int = 5) -> list[KBArticle]:
    """Deflection: active articles with any comma-separated keyword appearing
    (case-insensitive substring) in the customer's draft text. Ranked by number
    of distinct keyword hits. Plain matching, no index — sufficient at this
    scale (see README)."""
    haystack = text.lower()
    scored: list[tuple[int, KBArticle]] = []
    for article in db.scalars(select(KBArticle).where(KBArticle.active == True)):  # noqa: E712
        keywords = [k.strip().lower() for k in article.keywords.split(",") if k.strip()]
        hits = sum(1 for k in keywords if k in haystack)
        if hits:
            scored.append((hits, article))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [article for _, article in scored[:limit]]


# ---- Bulk actions & merge (Phase 5) ----


def bulk_update_tickets(db: Session, ticket_ids: list[str], updates: dict, actor: User) -> list[str]:
    """Applies the same field updates to many tickets in one commit, logging a
    per-ticket event and (for status changes) firing customer notifications —
    reuses the single-ticket update_ticket semantics by calling it per row so
    SLA recompute / notification side effects stay consistent. Unknown ticket
    ids are silently skipped; returns the ids actually updated."""
    updated: list[str] = []
    for ticket_id in ticket_ids:
        ticket = db.get(Ticket, ticket_id)
        if ticket is None:
            continue
        update_ticket(db, ticket, dict(updates), actor)
        updated.append(ticket_id)
    return updated


def merge_tickets(db: Session, target: Ticket, source_ids: list[str], actor: User) -> list[str]:
    """Marks each source as a duplicate of `target`: sets merged_into_id,
    closes it, and logs events on both sides. Skips ids that don't exist, are
    the target itself, or are already merged. Returns the ids actually merged."""
    merged: list[str] = []
    for source_id in source_ids:
        if source_id == target.id:
            continue
        source = db.get(Ticket, source_id)
        if source is None or source.merged_into_id is not None:
            continue
        source.merged_into_id = target.id
        source.status = TicketStatus.CLOSED
        log_event(db, source, "merged_into", actor, {"target_ticket_id": target.id})
        log_event(db, target, "merge_received", actor, {"source_ticket_id": source_id})
        merged.append(source_id)
    if merged:
        db.commit()
        db.refresh(target)
    return merged


# ---- Presence / collision detection (Phase 5) ----


def touch_presence(db: Session, ticket_id: str, user_id: str) -> TicketPresence:
    """Upsert this (ticket, user) heartbeat to now. One row per pair via the
    unique constraint — find-or-create rather than relying on a DB upsert so
    it's portable across SQLite and Postgres."""
    presence = db.scalar(
        select(TicketPresence).where(
            TicketPresence.ticket_id == ticket_id, TicketPresence.user_id == user_id
        )
    )
    if presence is None:
        presence = TicketPresence(ticket_id=ticket_id, user_id=user_id, last_seen_at=_now())
        db.add(presence)
    else:
        presence.last_seen_at = _now()
    db.commit()
    db.refresh(presence)
    return presence


def list_active_presence(
    db: Session, ticket_id: str, window_seconds: int, exclude_user_id: str | None = None
) -> list[TicketPresence]:
    """Presence rows for a ticket seen within the recent window (i.e. viewers
    currently on it), optionally excluding the caller so they only see *others*."""
    cutoff = _now() - timedelta(seconds=window_seconds)
    rows = db.scalars(
        select(TicketPresence)
        .where(TicketPresence.ticket_id == ticket_id)
        .order_by(TicketPresence.last_seen_at.desc())
    )
    active = []
    for row in rows:
        if exclude_user_id is not None and row.user_id == exclude_user_id:
            continue
        if as_aware_utc(row.last_seen_at) >= cutoff:
            active.append(row)
    return active
