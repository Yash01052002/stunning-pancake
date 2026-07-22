"""Auto-triage engine: rule-based (Phase 2) and LLM-based (Phase 3).

v1/v2 design (see docs/support-ticket-system-master-plan.md, Phases 2-3):
- rule engine: keyword rules assign category + priority + team
- LLM engine: Claude classifies category/sentiment/priority/confidence;
  category is routed to a team by reusing the same triage_rules table as
  a category -> team map, so rule authors don't maintain two mappings
- premium customers get a one-level priority boost; angry-sentiment
  tickets (LLM engine only) get the same boost
- the least-loaded active agent on the assigned team gets the ticket
- unmatched / low-confidence tickets fall back to a configurable "Triage"
  team for human review, and are never auto-assigned to an agent
- which engine runs is a config default (settings.auto_triage_engine),
  overridable per-call for A/B comparison (POST /tickets/{id}/triage?engine=)
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.crud import log_event
from app.llm_classifier import get_classifier
from app.models import (
    CustomerTier,
    SentimentLabel,
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


def find_team_for_category(db: Session, category: str) -> Team | None:
    """LLM classifications route to a team by matching an active rule's
    category (case-insensitive) — reuses the Phase 2 rule table as the
    category -> team map instead of maintaining a second one."""
    rule = db.scalar(
        select(TriageRule)
        .where(TriageRule.active == True, func.lower(TriageRule.category) == category.lower())  # noqa: E712
        .order_by(TriageRule.evaluation_order)
    )
    return rule.team if rule else None


def boost_priority_for_tier(priority: TicketPriority, tier: CustomerTier | None) -> TicketPriority:
    """Premium customers get bumped one priority level (capped at P0)."""
    if tier != CustomerTier.PREMIUM:
        return priority
    idx = _PRIORITY_ORDER.index(priority)
    return _PRIORITY_ORDER[max(idx - 1, 0)]


def boost_priority_for_sentiment(
    priority: TicketPriority, sentiment: SentimentLabel | None
) -> TicketPriority:
    """Angry customers get bumped one priority level (capped at P0)."""
    if sentiment != SentimentLabel.ANGRY:
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


def _route_to_fallback_queue(
    db: Session, ticket: Ticket, actor: User | None, outcome: TriageOutcome, event_payload: dict
) -> Ticket:
    """Unmatched (either engine) and low-confidence (LLM) tickets land here:
    routed to the human fallback team, never auto-assigned to an agent."""
    ticket.triage_outcome = outcome
    fallback_team = get_fallback_team(db)
    if fallback_team is not None:
        ticket.assigned_team_id = fallback_team.id
    log_event(
        db,
        ticket,
        "auto_triaged",
        actor,
        {**event_payload, "fallback_team_id": fallback_team.id if fallback_team else None},
    )
    db.commit()
    db.refresh(ticket)
    return ticket


def _run_rule_triage(db: Session, ticket: Ticket, actor: User | None) -> Ticket:
    rule = evaluate_rules(db, ticket)
    if rule is None:
        return _route_to_fallback_queue(
            db, ticket, actor, TriageOutcome.UNMATCHED, {"engine": "rule", "matched": False}
        )

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
            "engine": "rule",
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


def _run_llm_triage(db: Session, ticket: Ticket, actor: User | None) -> Ticket:
    result = get_classifier().classify(ticket.subject, ticket.body)

    if result is None:
        return _route_to_fallback_queue(
            db,
            ticket,
            actor,
            TriageOutcome.UNMATCHED,
            {"engine": "llm", "matched": False, "reason": "classifier_unavailable"},
        )

    sentiment = SentimentLabel(result.sentiment)
    priority = TicketPriority(result.priority)
    priority = boost_priority_for_tier(priority, ticket.customer.tier)
    priority = boost_priority_for_sentiment(priority, sentiment)

    # the classification itself is stored either way — even a low-confidence
    # guess is useful context for the human who picks this up
    ticket.category = result.category
    ticket.priority = priority
    ticket.sentiment = sentiment
    ticket.confidence_score = result.confidence

    if result.confidence < settings.llm_confidence_threshold:
        # low confidence: don't auto-assign, don't claim ownership — send to a human
        return _route_to_fallback_queue(
            db,
            ticket,
            actor,
            TriageOutcome.LOW_CONFIDENCE,
            {
                "engine": "llm",
                "matched": True,
                "category": result.category,
                "sentiment": sentiment.value,
                "priority": priority.value,
                "confidence": result.confidence,
                "rationale": result.rationale,
                "reason": "low_confidence",
            },
        )

    team = find_team_for_category(db, result.category)
    agent = pick_agent_for_team(db, team.id) if team else None

    ticket.assigned_team_id = team.id if team else None
    ticket.assigned_agent_id = agent.id if agent else None
    ticket.triage_outcome = TriageOutcome.MATCHED
    ticket.triage_method = TriageMethod.LLM

    log_event(
        db,
        ticket,
        "auto_triaged",
        actor,
        {
            "engine": "llm",
            "matched": True,
            "category": result.category,
            "sentiment": sentiment.value,
            "priority": priority.value,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "assigned_team_id": team.id if team else None,
            "assigned_agent_id": agent.id if agent else None,
        },
    )
    db.commit()
    db.refresh(ticket)
    return ticket


def run_auto_triage(
    db: Session, ticket: Ticket, actor: User | None = None, engine: str | None = None
) -> Ticket:
    """Runs the configured (or explicitly requested) triage engine.

    `engine` overrides settings.auto_triage_engine for this call only —
    used by POST /tickets/{id}/triage?engine=llm|rule for A/B comparison
    without needing to flip the global default.
    """
    engine = engine or settings.auto_triage_engine
    if engine == "llm":
        return _run_llm_triage(db, ticket, actor)
    return _run_rule_triage(db, ticket, actor)
