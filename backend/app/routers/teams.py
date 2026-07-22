from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import require_admin, require_staff
from app.schemas import TeamCreate, TeamRead

router = APIRouter(prefix="/teams", tags=["teams"])


@router.post("", response_model=TeamRead, status_code=status.HTTP_201_CREATED)
def create_team(
    payload: TeamCreate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    return crud.create_team(db, payload.name)


@router.get("", response_model=list[TeamRead])
def list_teams(db: Session = Depends(get_db), _=Depends(require_staff)):
    return crud.list_teams(db)
