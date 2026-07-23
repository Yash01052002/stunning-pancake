from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crud
from app.config import settings
from app.database import get_db
from app.deps import require_admin, require_staff
from app.models import Ticket, TriageRule
from app.schemas import AuditLogRead, CategoriesReport

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/categories", response_model=CategoriesReport)
def list_categories(db: Session = Depends(get_db), _=Depends(require_staff)):
    """Taxonomy view: the categories the LLM classifies into (config) alongside
    the distinct categories actually present on rules and tickets. Surfaces
    drift — e.g. a rule or LLM classification using a category not in the
    configured set — so admins can reconcile without reading the DB directly."""
    rule_categories = set(db.scalars(select(TriageRule.category).distinct()))
    ticket_categories = {
        c for c in db.scalars(select(Ticket.category).distinct()) if c is not None
    }
    in_use = sorted(rule_categories | ticket_categories)
    return CategoriesReport(configured=settings.triage_categories, in_use=in_use)


@router.get("/audit-logs", response_model=list[AuditLogRead])
def list_audit_logs(
    limit: int = 100,
    actor_id: str | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Admin-only security audit trail of state-changing requests, newest first."""
    return crud.list_audit_logs(db, limit=min(limit, 500), actor_id=actor_id)
