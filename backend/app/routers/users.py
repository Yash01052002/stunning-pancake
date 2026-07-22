from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import require_admin, require_staff
from app.models import User, UserRole
from app.schemas import UserCreate, UserRead

router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_staff_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Admin-only: create agent/admin/customer accounts directly."""
    if crud.get_user_by_email(db, payload.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    return crud.create_user(
        db,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        role=payload.role,
        tier=payload.tier,
        team_id=payload.team_id,
    )


@router.get("", response_model=list[UserRead])
def list_users(
    role: UserRole | None = None,
    db: Session = Depends(get_db),
    _=Depends(require_staff),
):
    stmt = select(User)
    if role is not None:
        stmt = stmt.where(User.role == role)
    return list(db.scalars(stmt))
