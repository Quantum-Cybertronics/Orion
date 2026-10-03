from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.backend.auth.session import SESSION_COOKIE_NAME, get_user_id
from app.backend.database import get_db
from app.backend.models import User


def _not_authenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated.",
    )


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    """Return the user identified by the session cookie, or raise 401.

    Every route that needs a logged-in user should depend on this, so the
    authentication rules live in exactly one place.
    """
    session_token = request.cookies.get(SESSION_COOKIE_NAME)

    if not session_token:
        raise _not_authenticated()

    user_id = get_user_id(session_token)

    if user_id is None:
        raise _not_authenticated()

    user = db.get(User, user_id)

    if user is None:
        raise _not_authenticated()

    return user
