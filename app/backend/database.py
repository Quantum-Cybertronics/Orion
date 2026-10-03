from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.backend.config import DATA_DIR


# The database lives in the data directory, outside the source code.
DATABASE_URL = f"sqlite:///{DATA_DIR / 'orion.db'}"


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    from app.backend import models

    Base.metadata.create_all(bind=engine)