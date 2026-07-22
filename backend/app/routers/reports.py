from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_staff
from app.models import Ticket, TriageMethod, TriageOutcome
from app.schemas import TriageAccuracyReport

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
