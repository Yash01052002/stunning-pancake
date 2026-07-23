from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import require_admin, require_staff
from app.schemas import CannedResponseCreate, CannedResponseRead, CannedResponseUpdate

router = APIRouter(prefix="/canned-responses", tags=["canned-responses"])


@router.post("", response_model=CannedResponseRead, status_code=status.HTTP_201_CREATED)
def create_canned_response(
    payload: CannedResponseCreate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    return crud.create_canned_response(db, **payload.model_dump())


@router.get("", response_model=list[CannedResponseRead])
def list_canned_responses(
    category: str | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_staff),
):
    return crud.list_canned_responses(db, category=category)


@router.patch("/{canned_id}", response_model=CannedResponseRead)
def update_canned_response(
    canned_id: str,
    payload: CannedResponseUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    canned = crud.get_canned_response(db, canned_id)
    if canned is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Canned response not found")
    return crud.update_canned_response(db, canned, payload.model_dump(exclude_unset=True))
