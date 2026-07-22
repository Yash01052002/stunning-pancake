from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import require_admin, require_staff
from app.schemas import TriageRuleCreate, TriageRuleRead, TriageRuleUpdate

router = APIRouter(prefix="/triage-rules", tags=["triage-rules"])


@router.post("", response_model=TriageRuleRead, status_code=status.HTTP_201_CREATED)
def create_triage_rule(
    payload: TriageRuleCreate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    return crud.create_triage_rule(db, **payload.model_dump())


@router.get("", response_model=list[TriageRuleRead])
def list_triage_rules(db: Session = Depends(get_db), _=Depends(require_staff)):
    return crud.list_triage_rules(db)


@router.patch("/{rule_id}", response_model=TriageRuleRead)
def update_triage_rule(
    rule_id: str,
    payload: TriageRuleUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    rule = crud.get_triage_rule(db, rule_id)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Triage rule not found")
    updates = payload.model_dump(exclude_unset=True)
    return crud.update_triage_rule(db, rule, updates)
