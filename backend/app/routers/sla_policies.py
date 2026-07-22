from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud, sla
from app.database import get_db
from app.deps import require_admin, require_staff
from app.schemas import SLAEscalationReport, SLAPolicyCreate, SLAPolicyRead, SLAPolicyUpdate

router = APIRouter(tags=["sla"])


@router.post("/sla-policies", response_model=SLAPolicyRead, status_code=status.HTTP_201_CREATED)
def create_sla_policy(
    payload: SLAPolicyCreate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    return crud.create_sla_policy(db, **payload.model_dump())


@router.get("/sla-policies", response_model=list[SLAPolicyRead])
def list_sla_policies(db: Session = Depends(get_db), _=Depends(require_staff)):
    return crud.list_sla_policies(db)


@router.patch("/sla-policies/{policy_id}", response_model=SLAPolicyRead)
def update_sla_policy(
    policy_id: str,
    payload: SLAPolicyUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    policy = crud.get_sla_policy(db, policy_id)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SLA policy not found")
    updates = payload.model_dump(exclude_unset=True)
    return crud.update_sla_policy(db, policy, updates)


@router.post("/sla/escalate", response_model=SLAEscalationReport)
def run_sla_escalation_check(db: Session = Depends(get_db), _=Depends(require_staff)):
    """Idempotent — safe to invoke repeatedly (manually, or from an external
    cron/scheduler hitting this endpoint; see scripts/run_sla_escalations.py
    for the same check run as a standalone script)."""
    return sla.check_and_escalate_slas(db)
