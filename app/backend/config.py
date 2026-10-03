import os
import secrets
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]

# ORION_DATA_DIR lets tests (or a launcher) relocate all mutable data.
DATA_DIR = Path(os.getenv("ORION_DATA_DIR") or BASE_DIR / "data").resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_SECRET_FILENAME = "session_secret"


def load_session_secret(data_dir: Path = DATA_DIR) -> str:
    """Return the key used to sign session cookies.

    Priority:
      1. ORION_SESSION_SECRET environment variable (explicit override).
      2. A secret previously generated and stored in the data directory.
      3. A new random secret, generated once and saved with owner-only
         permissions so it survives restarts (and travels with the USB).
    """
    from_env = os.getenv("ORION_SESSION_SECRET")

    if from_env:
        return from_env

    secret_path = data_dir / SESSION_SECRET_FILENAME

    for _ in range(2):
        if secret_path.exists():
            existing = secret_path.read_text(encoding="utf-8").strip()

            if existing:
                return existing

        new_secret = secrets.token_urlsafe(64)

        try:
            # O_EXCL: never overwrite a secret another process just wrote.
            fd = os.open(
                secret_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            continue

        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(new_secret)

        return new_secret

    raise RuntimeError(f"Could not load or create {secret_path}.")
