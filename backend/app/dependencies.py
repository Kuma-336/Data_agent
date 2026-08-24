from fastapi import Cookie, Depends, HTTPException, status
from sqlalchemy.orm import Session
from .db import get_db
from .models import User
from .security import decode_access_token


def current_user(
    access_token: str | None = Cookie(default=None), db: Session = Depends(get_db)
) -> User:
    if not access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        user_id = decode_access_token(access_token)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from exc
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user
