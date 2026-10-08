from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings
from app.db_types import register_integer_id_assignment

if settings.use_databricks:
    engine = create_engine(settings.databricks_url(), pool_pre_ping=True)
else:
    engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


register_integer_id_assignment(Base)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
