from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import require_admin, require_staff
from app.schemas import TeamCreate, TeamRead, TeamUpdate

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


@router.patch("/{team_id}", response_model=TeamRead)
def rename_team(
    team_id: str,
    payload: TeamUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    team = crud.get_team(db, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return crud.rename_team(db, team, payload.name)
