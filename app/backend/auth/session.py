from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.backend.config import load_session_secret


SESSION_COOKIE_NAME = "orion_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days

_serializer = URLSafeTimedSerializer(load_session_secret())


def create_session(user_id: str) -> str:
    return _serializer.dumps({"user_id": user_id})


def get_user_id(session_token: str) -> str | None:
    try:
        data = _serializer.loads(
            session_token,
            max_age=SESSION_MAX_AGE,
        )
    except (BadSignature, SignatureExpired):
        return None

    user_id = data.get("user_id")

    if not isinstance(user_id, str) or not user_id:
        return None

    return user_id
