"""Phase 6: reporting & analytics.

Pure query/aggregation functions that return the report schema objects. Kept
out of the router so the same computations back both the JSON dashboards and
the CSV exports, and so they're unit-testable without HTTP.

SLA compliance reuses Ticket.first_response_sla_status /
.resolution_sla_status (computed-on-read in app/models.py) rather than
re-deriving breach logic here — one source of truth for what "breached" means.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Ticket, TicketPriority, TicketStatus, TriageOutcome, User, UserRole
from app.schemas import (
    AgentWorkloadReport,
    AgentWorkloadRow,
    CategoryCount,
    CSATReport,
    SLAComplianceReport,
    TriageTrendBucket,
    TriageTrendReport,
    VolumeReport,
)

_OPEN_STATUSES = (TicketStatus.NEW, TicketStatus.OPEN, TicketStatus.PENDING)


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Normalize an incoming filter bound to naive-UTC so it compares
    correctly against SQLite's naive-UTC storage (Postgres with a UTC session
    handles this equivalently). Mirrors the app's storage convention."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _date_filtered(stmt, created_from: datetime | None, created_to: datetime | None):
    created_from, created_to = _naive_utc(created_from), _naive_utc(created_to)
    if created_from is not None:
        stmt = stmt.where(Ticket.created_at >= created_from)
    if created_to is not None:
        stmt = stmt.where(Ticket.created_at <= created_to)
    return stmt


def _tickets(db: Session, created_from, created_to) -> list[Ticket]:
    return list(db.scalars(_date_filtered(select(Ticket), created_from, created_to)))


def volume_report(db: Session, created_from=None, created_to=None) -> VolumeReport:
    tickets = _tickets(db, created_from, created_to)
    by_category: Counter = Counter()
    by_status: Counter = Counter()
    by_priority: Counter = Counter()
    for t in tickets:
        by_category[t.category] += 1
        by_status[t.status.value] += 1
        by_priority[t.priority.value if t.priority else "unset"] += 1

    # deterministic ordering: highest volume first, then category name
    ordered = sorted(by_category.items(), key=lambda kv: (-kv[1], (kv[0] or "")))
    return VolumeReport(
        total=len(tickets),
        by_category=[CategoryCount(category=c, count=n) for c, n in ordered],
        by_status=dict(by_status),
        by_priority=dict(by_priority),
    )


def sla_compliance_report(db: Session, created_from=None, created_to=None) -> SLAComplianceReport:
    tickets = _tickets(db, created_from, created_to)

    def tally(status_values: list[str | None]) -> tuple[int, int, int]:
        met = sum(1 for s in status_values if s == "met")
        breached = sum(1 for s in status_values if s in ("breached", "breached_late"))
        pending = sum(1 for s in status_values if s in ("on_track", "at_risk"))
        return met, breached, pending

    fr_met, fr_breached, fr_pending = tally([t.first_response_sla_status for t in tickets])
    res_met, res_breached, res_pending = tally([t.resolution_sla_status for t in tickets])

    def rate(met: int, breached: int) -> float:
        decided = met + breached
        return met / decided if decided else 0.0

    return SLAComplianceReport(
        first_response_met=fr_met,
        first_response_breached=fr_breached,
        first_response_pending=fr_pending,
        first_response_compliance_rate=rate(fr_met, fr_breached),
        resolution_met=res_met,
        resolution_breached=res_breached,
        resolution_pending=res_pending,
        resolution_compliance_rate=rate(res_met, res_breached),
    )


def csat_report(db: Session, created_from=None, created_to=None) -> CSATReport:
    stmt = _date_filtered(
        select(Ticket).where(Ticket.csat_rating.isnot(None)), created_from, created_to
    )
    ratings = [t.csat_rating for t in db.scalars(stmt)]
    distribution = {r: 0 for r in range(1, 6)}
    for r in ratings:
        distribution[r] = distribution.get(r, 0) + 1
    average = sum(ratings) / len(ratings) if ratings else None
    return CSATReport(responses=len(ratings), average_rating=average, distribution=distribution)


def agent_workload_report(db: Session) -> AgentWorkloadReport:
    """Live snapshot (not date-filtered): current open/resolved/total counts
    per active agent."""
    agents = db.scalars(
        select(User).where(User.role == UserRole.AGENT).order_by(User.full_name)
    )
    rows = []
    for agent in agents:
        open_count = (
            db.scalar(
                select(func.count(Ticket.id)).where(
                    Ticket.assigned_agent_id == agent.id, Ticket.status.in_(_OPEN_STATUSES)
                )
            )
            or 0
        )
        resolved_count = (
            db.scalar(
                select(func.count(Ticket.id)).where(
                    Ticket.assigned_agent_id == agent.id, Ticket.status == TicketStatus.RESOLVED
                )
            )
            or 0
        )
        total = db.scalar(
            select(func.count(Ticket.id)).where(Ticket.assigned_agent_id == agent.id)
        ) or 0
        rows.append(
            AgentWorkloadRow(
                agent_id=agent.id,
                full_name=agent.full_name,
                open_tickets=open_count,
                resolved_tickets=resolved_count,
                total_assigned=total,
            )
        )
    return AgentWorkloadReport(agents=rows)


def triage_trend_report(db: Session, created_from=None, created_to=None) -> TriageTrendReport:
    """Auto-triage success rate bucketed by creation day, so a dashboard can
    plot the accuracy trend over time (the master plan's exit-criteria chart)."""
    from app.models import TriageMethod

    tickets = _tickets(db, created_from, created_to)
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "matched": 0, "overridden": 0})
    for t in tickets:
        day = as_iso_day(t.created_at)
        b = buckets[day]
        b["total"] += 1
        if t.triage_outcome == TriageOutcome.MATCHED:
            b["matched"] += 1
            if t.triage_method == TriageMethod.MANUAL:
                b["overridden"] += 1

    result = []
    for day in sorted(buckets):
        b = buckets[day]
        success = (b["matched"] - b["overridden"]) / b["total"] if b["total"] else 0.0
        result.append(
            TriageTrendBucket(
                period=day,
                total=b["total"],
                matched=b["matched"],
                overridden=b["overridden"],
                auto_triage_success_rate=success,
            )
        )
    return TriageTrendReport(buckets=result)


def as_iso_day(dt: datetime) -> str:
    return dt.date().isoformat()
