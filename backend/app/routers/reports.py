import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import analytics
from app.database import get_db
from app.deps import require_staff
from app.models import Ticket, TriageMethod, TriageOutcome
from app.schemas import (
    AgentWorkloadReport,
    CSATReport,
    SLAComplianceReport,
    TriageAccuracyReport,
    TriageTrendReport,
    VolumeReport,
)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/triage-accuracy", response_model=TriageAccuracyReport)
def triage_accuracy(db: Session = Depends(get_db), _=Depends(require_staff)):
    total_tickets = db.scalar(select(func.count(Ticket.id))) or 0
    matched = (
        db.scalar(
            select(func.count(Ticket.id)).where(Ticket.triage_outcome == TriageOutcome.MATCHED)
        )
        or 0
    )
    unmatched = (
        db.scalar(
            select(func.count(Ticket.id)).where(Ticket.triage_outcome == TriageOutcome.UNMATCHED)
        )
        or 0
    )
    # "overridden": the rule engine matched it, but a human has since changed
    # category/priority/assignment — this is the signal the exit criteria
    # ("auto-categorized and routed without agent correction") is built on.
    overridden = (
        db.scalar(
            select(func.count(Ticket.id)).where(
                Ticket.triage_outcome == TriageOutcome.MATCHED,
                Ticket.triage_method == TriageMethod.MANUAL,
            )
        )
        or 0
    )

    coverage_rate = matched / total_tickets if total_tickets else 0.0
    accuracy_rate = (matched - overridden) / matched if matched else 0.0
    auto_triage_success_rate = (matched - overridden) / total_tickets if total_tickets else 0.0

    return TriageAccuracyReport(
        total_tickets=total_tickets,
        matched=matched,
        unmatched=unmatched,
        overridden=overridden,
        coverage_rate=coverage_rate,
        accuracy_rate=accuracy_rate,
        auto_triage_success_rate=auto_triage_success_rate,
    )


@router.get("/volume", response_model=VolumeReport)
def volume(
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_staff),
):
    return analytics.volume_report(db, created_from, created_to)


@router.get("/sla-compliance", response_model=SLAComplianceReport)
def sla_compliance(
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_staff),
):
    return analytics.sla_compliance_report(db, created_from, created_to)


@router.get("/csat", response_model=CSATReport)
def csat(
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_staff),
):
    return analytics.csat_report(db, created_from, created_to)


@router.get("/agent-workload", response_model=AgentWorkloadReport)
def agent_workload(db: Session = Depends(get_db), _=Depends(require_staff)):
    return analytics.agent_workload_report(db)


@router.get("/triage-trend", response_model=TriageTrendReport)
def triage_trend(
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_staff),
):
    return analytics.triage_trend_report(db, created_from, created_to)


# ---- CSV exports (leadership) ----


def _csv_response(header: list[str], rows: list[list], filename: str) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_TICKET_EXPORT_HEADER = [
    "id", "subject", "customer_id", "category", "priority", "status",
    "assigned_agent_id", "assigned_team_id", "triage_method", "triage_outcome",
    "first_response_sla_status", "resolution_sla_status", "csat_rating",
    "created_at", "resolved_at",
]


@router.get("/export/tickets.csv")
def export_tickets_csv(
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_staff),
):
    """Raw ticket dump for leadership / offline analysis (date-filterable)."""
    tickets = analytics._tickets(db, created_from, created_to)
    rows = [
        [
            t.id, t.subject, t.customer_id, t.category or "",
            t.priority.value if t.priority else "", t.status.value,
            t.assigned_agent_id or "", t.assigned_team_id or "",
            t.triage_method.value if t.triage_method else "",
            t.triage_outcome.value if t.triage_outcome else "",
            t.first_response_sla_status or "", t.resolution_sla_status or "",
            t.csat_rating if t.csat_rating is not None else "",
            t.created_at.isoformat() if t.created_at else "",
            t.resolved_at.isoformat() if t.resolved_at else "",
        ]
        for t in tickets
    ]
    return _csv_response(_TICKET_EXPORT_HEADER, rows, "tickets.csv")


@router.get("/export/volume.csv")
def export_volume_csv(
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_staff),
):
    """Volume-by-category as CSV — the most common leadership export."""
    report = analytics.volume_report(db, created_from, created_to)
    rows = [[c.category or "(uncategorized)", c.count] for c in report.by_category]
    return _csv_response(["category", "count"], rows, "volume_by_category.csv")
