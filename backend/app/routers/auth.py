from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud
from app.config import settings
from app.database import get_db
from app.deps import get_current_user
from app.models import User, UserRole
from app.rate_limit import rate_limit
from app.schemas import LoginRequest, TokenResponse, UserCreate, UserRead
from app.security import create_access_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

_auth_rate_limit = rate_limit("auth", lambda: settings.rate_limit_auth_per_minute)


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[_auth_rate_limit],
)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    """Public self-service signup. Always creates a CUSTOMER account —
    staff accounts (agent/admin) are created via POST /users by an admin."""
    if crud.get_user_by_email(db, payload.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    user = crud.create_user(
        db,
        email=payload.email,
        password=payload.password,
        full_name=payload.full_name,
        role=UserRole.CUSTOMER,
        tier=payload.tier,
        team_id=None,
    )
    return user


@router.post("/login", response_model=TokenResponse, dependencies=[_auth_rate_limit])
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = crud.get_user_by_email(db, payload.email)
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if not user.active:
        raise HTTPException(status_code=403, detail="User is inactive")
    token = create_access_token(subject=user.id)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserRead)
def me(current_user: User = Depends(get_current_user)):
    return current_user
