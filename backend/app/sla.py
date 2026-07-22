"""Phase 4: SLA timers and breach escalation.

Two independent clocks per ticket, both set from whichever SLAPolicy
matches (priority, customer tier) once the ticket has a priority:
- first_response_due_at: stopped by the first public staff comment
- resolution_due_at: stopped by status -> resolved

Both clocks' current state (on_track/at_risk/breached/met/breached_late)
are computed on read via Ticket.first_response_sla_status /
.resolution_sla_status (see app/models.py) — always fresh, no polling
needed just to *see* a breach.

Escalation is the "do something about it" half: check_and_escalate_slas
bumps priority, reassigns to the least-loaded agent on the team, and
notifies — idempotent via ticket.escalated_at, so it's safe to invoke
repeatedly from a cron-style trigger (see scripts/run_sla_escalations.py
and POST /sla/escalate) without an in-process scheduler. A background
asyncio loop was considered and rejected: with multiple API workers each
would run its own loop and could double-escalate the same ticket, whereas
an idempotent externally-triggered endpoint works the same under any
number of workers.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notifications
from app.models import (
    CustomerTier,
    SLAPolicy,
    Ticket,
    TicketEvent,
    TicketPriority,
    TicketStatus,
    User,
    as_aware_utc,
    bump_priority,
)

_OPEN_STATUSES = (TicketStatus.NEW, TicketStatus.OPEN, TicketStatus.PENDING)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def find_sla_policy(
    db: Session, priority: TicketPriority, tier: CustomerTier | None
) -> SLAPolicy | None:
    """Most-specific match wins: a policy scoped to this exact tier beats a
    tier=null (applies-to-any-tier) policy for the same priority."""
    if tier is not None:
        specific = db.scalar(
            select(SLAPolicy).where(
                SLAPolicy.active == True,  # noqa: E712
                SLAPolicy.priority == priority,
                SLAPolicy.tier == tier,
            )
        )
        if specific is not None:
            return specific
    return db.scalar(
        select(SLAPolicy).where(
            SLAPolicy.active == True, SLAPolicy.priority == priority, SLAPolicy.tier.is_(None)  # noqa: E712
        )
    )


def apply_sla_targets(db: Session, ticket: Ticket) -> None:
    """Sets/recomputes first_response_due_at and resolution_due_at from the
    matching policy. No-op if the ticket has no priority yet or no policy
    matches. Called by both triage engines on a match, and by manual
    PATCH when staff sets/changes priority."""
    if ticket.priority is None:
        return
    policy = find_sla_policy(db, ticket.priority, ticket.customer.tier)
    if policy is None:
        return
    base = as_aware_utc(ticket.created_at)
    ticket.first_response_due_at = base + timedelta(minutes=policy.first_response_minutes)
    ticket.resolution_due_at = base + timedelta(minutes=policy.resolution_minutes)


def check_and_escalate_slas(db: Session) -> dict:
    """Idempotent: only considers tickets not yet escalated
    (escalated_at is None) that are still open. Safe to call repeatedly —
    see the module docstring for why this replaces an in-process scheduler.
    """
    from app import triage  # local import: avoids a triage<->sla module-level cycle

    now = _now()
    candidates = db.scalars(
        select(Ticket).where(Ticket.status.in_(_OPEN_STATUSES), Ticket.escalated_at.is_(None))
    )

    escalated_ids = []
    for ticket in candidates:
        breach_reason = None
        if (
            ticket.first_response_due_at is not None
            and ticket.first_responded_at is None
            and as_aware_utc(ticket.first_response_due_at) < now
        ):
            breach_reason = "first_response"
        elif (
            ticket.resolution_due_at is not None
            and as_aware_utc(ticket.resolution_due_at) < now
        ):
            breach_reason = "resolution"

        if breach_reason is None:
            continue

        old_priority = ticket.priority
        if ticket.priority is not None:
            ticket.priority = bump_priority(ticket.priority)

        old_agent_id = ticket.assigned_agent_id
        if ticket.assigned_team_id:
            agent = triage.pick_agent_for_team(db, ticket.assigned_team_id)
            if agent is not None:
                ticket.assigned_agent_id = agent.id

        ticket.escalated_at = now
        db.add(
            TicketEvent(
                ticket_id=ticket.id,
                type="sla_escalated",
                actor_id=None,
                payload={
                    "reason": breach_reason,
                    "old_priority": old_priority.value if old_priority else None,
                    "new_priority": ticket.priority.value if ticket.priority else None,
                    "old_agent_id": old_agent_id,
                    "new_agent_id": ticket.assigned_agent_id,
                },
            )
        )

        assigned_agent = (
            db.get(User, ticket.assigned_agent_id) if ticket.assigned_agent_id else None
        )
        notifications.notify_sla_escalation(db, ticket, assigned_agent, breach_reason)

        db.commit()
        db.refresh(ticket)
        escalated_ids.append(ticket.id)

    return {
        "checked_at": now,
        "escalated_ticket_ids": escalated_ids,
        "escalated_count": len(escalated_ids),
    }
