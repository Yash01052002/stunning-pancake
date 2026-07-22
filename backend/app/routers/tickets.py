from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import get_current_user, require_staff
from app.models import Ticket, TicketPriority, TicketStatus, User, UserRole
from app.schemas import TicketCreate, TicketDetailRead, TicketRead, TicketUpdate

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _get_ticket_or_404(db: Session, ticket_id: str) -> Ticket:
    ticket = crud.get_ticket(db, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return ticket


def _ensure_can_view(ticket: Ticket, user: User) -> None:
    if user.role == UserRole.CUSTOMER and ticket.customer_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions")


@router.post("", response_model=TicketRead, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role == UserRole.CUSTOMER:
        customer_id = current_user.id
    else:
        # agents/admins may open a ticket on behalf of a customer
        if not payload.customer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer_id is required when staff creates a ticket on behalf of a customer",
            )
        customer_id = payload.customer_id

    return crud.create_ticket(
        db,
        customer_id=customer_id,
        subject=payload.subject,
        body=payload.body,
        channel=payload.channel,
        actor=current_user,
    )


@router.get("", response_model=list[TicketRead])
def list_tickets(
    status_filter: TicketStatus | None = None,
    category: str | None = None,
    priority: TicketPriority | None = None,
    assigned_team_id: str | None = None,
    assigned_agent_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    customer_id = current_user.id if current_user.role == UserRole.CUSTOMER else None
    return crud.list_tickets(
        db,
        customer_id=customer_id,
        status=status_filter,
        category=category,
        priority=priority,
        assigned_team_id=assigned_team_id,
        assigned_agent_id=assigned_agent_id,
    )


@router.get("/{ticket_id}", response_model=TicketDetailRead)
def get_ticket(
    ticket_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    _ensure_can_view(ticket, current_user)
    # customers must not see internal-only comments
    if current_user.role == UserRole.CUSTOMER:
        ticket.comments = [c for c in ticket.comments if not c.is_internal]
    return ticket


@router.patch("/{ticket_id}", response_model=TicketRead)
def update_ticket(
    ticket_id: str,
    payload: TicketUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_staff),
):
    ticket = _get_ticket_or_404(db, ticket_id)
    updates = payload.model_dump(exclude_unset=True)
    return crud.update_ticket(db, ticket, updates, actor=current_user)
