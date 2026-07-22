"""Phase 2: rule-based auto-triage engine.

v1 design (see docs/support-ticket-system-master-plan.md, Phase 2):
- keyword rules assign category + priority + team
- premium customers get a one-level priority boost
- the least-loaded active agent on the assigned team gets the ticket
- unmatched tickets fall back to a configurable "Triage" team for human review
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.crud import log_event
from app.models import (
    CustomerTier,
    Team,
    Ticket,
    TicketPriority,
    TicketStatus,
    TriageMethod,
    TriageOutcome,
    TriageRule,
    User,
    UserRole,
)

_OPEN_STATUSES = (TicketStatus.NEW, TicketStatus.OPEN, TicketStatus.PENDING)
_PRIORITY_ORDER = [TicketPriority.P0, TicketPriority.P1, TicketPriority.P2, TicketPriority.P3]


def get_fallback_team(db: Session) -> Team | None:
    return db.scalar(select(Team).where(Team.name == settings.fallback_triage_team_name))


def evaluate_rules(db: Session, ticket: Ticket) -> TriageRule | None:
    haystack = f"{ticket.subject}\n{ticket.body}".lower()
    rules = db.scalars(
        select(TriageRule)
        .where(TriageRule.active == True)  # noqa: E712
        .order_by(TriageRule.evaluation_order)
    )
    for rule in rules:
        if rule.keyword.lower() in haystack:
            return rule
    return None


def boost_priority_for_tier(priority: TicketPriority, tier: CustomerTier | None) -> TicketPriority:
    """Premium customers get bumped one priority level (capped at P0)."""
    if tier != CustomerTier.PREMIUM:
        return priority
    idx = _PRIORITY_ORDER.index(priority)
    return _PRIORITY_ORDER[max(idx - 1, 0)]


def pick_agent_for_team(db: Session, team_id: str) -> User | None:
    """Load-based routing: the active agent on the team with the fewest open tickets."""
    agents = list(
        db.scalars(
            select(User).where(
                User.role == UserRole.AGENT, User.team_id == team_id, User.active == True  # noqa: E712
            )
        )
    )
    if not agents:
        return None

    # per-agent count query; fine at v1 team sizes, a single grouped aggregate
    # is a reasonable optimization once team rosters grow large
    counts = {
        agent.id: db.scalar(
            select(func.count(Ticket.id)).where(
                Ticket.assigned_agent_id == agent.id, Ticket.status.in_(_OPEN_STATUSES)
            )
        )
        for agent in agents
    }
    return min(agents, key=lambda a: counts[a.id])


def run_auto_triage(db: Session, ticket: Ticket, actor: User | None = None) -> Ticket:
    rule = evaluate_rules(db, ticket)

    if rule is None:
        ticket.triage_outcome = TriageOutcome.UNMATCHED
        fallback_team = get_fallback_team(db)
        if fallback_team is not None:
            ticket.assigned_team_id = fallback_team.id
        log_event(
            db,
            ticket,
            "auto_triaged",
            actor,
            {"matched": False, "fallback_team_id": fallback_team.id if fallback_team else None},
        )
        db.commit()
        db.refresh(ticket)
        return ticket

    priority = boost_priority_for_tier(rule.priority, ticket.customer.tier)
    agent = pick_agent_for_team(db, rule.team_id) if rule.team_id else None

    ticket.category = rule.category
    ticket.priority = priority
    ticket.assigned_team_id = rule.team_id
    ticket.assigned_agent_id = agent.id if agent else None
    ticket.confidence_score = 1.0
    ticket.triage_outcome = TriageOutcome.MATCHED
    ticket.triage_method = TriageMethod.RULE

    log_event(
        db,
        ticket,
        "auto_triaged",
        actor,
        {
            "matched": True,
            "rule_id": rule.id,
            "rule_name": rule.name,
            "category": rule.category,
            "priority": priority.value,
            "assigned_team_id": rule.team_id,
            "assigned_agent_id": agent.id if agent else None,
        },
    )
    db.commit()
    db.refresh(ticket)
    return ticket
