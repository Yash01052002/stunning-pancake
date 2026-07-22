from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import get_current_user
from app.models import User, UserRole
from app.schemas import CommentCreate, CommentRead

router = APIRouter(prefix="/tickets/{ticket_id}/comments", tags=["comments"])


@router.post("", response_model=CommentRead, status_code=status.HTTP_201_CREATED)
def add_comment(
    ticket_id: str,
    payload: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    if current_user.role == UserRole.CUSTOMER:
        if ticket.customer_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
            )
        if payload.is_internal:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Customers cannot post internal notes",
            )

    return crud.add_comment(
        db, ticket, author=current_user, body=payload.body, is_internal=payload.is_internal
    )


@router.get("", response_model=list[CommentRead])
def list_comments(
    ticket_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    if current_user.role == UserRole.CUSTOMER and ticket.customer_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")

    comments = ticket.comments
    if current_user.role == UserRole.CUSTOMER:
        comments = [c for c in comments if not c.is_internal]
    return comments
