from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import crud
from app.database import get_db
from app.deps import get_current_user, require_admin, require_staff
from app.schemas import KBArticleCreate, KBArticleRead, KBArticleUpdate, KBSuggestRequest

router = APIRouter(tags=["kb"])


@router.post("/kb-articles", response_model=KBArticleRead, status_code=status.HTTP_201_CREATED)
def create_kb_article(
    payload: KBArticleCreate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    return crud.create_kb_article(db, **payload.model_dump())


@router.get("/kb-articles", response_model=list[KBArticleRead])
def list_kb_articles(db: Session = Depends(get_db), _=Depends(require_staff)):
    return crud.list_kb_articles(db)


@router.patch("/kb-articles/{article_id}", response_model=KBArticleRead)
def update_kb_article(
    article_id: str,
    payload: KBArticleUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    article = crud.get_kb_article(db, article_id)
    if article is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="KB article not found")
    return crud.update_kb_article(db, article, payload.model_dump(exclude_unset=True))


@router.post("/kb/suggest", response_model=list[KBArticleRead])
def suggest_kb_articles(
    payload: KBSuggestRequest,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Self-service deflection: a customer composing a ticket can call this
    with their draft subject/body to see relevant KB articles before
    submitting. Available to any authenticated user (customers included)."""
    text = f"{payload.subject}\n{payload.body}"
    return crud.suggest_kb_articles(db, text)
