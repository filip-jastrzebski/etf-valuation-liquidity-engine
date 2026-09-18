"""Database connection and engine management."""

import os
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def get_db_url() -> str:
    """Get the database URL from environment variables."""
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db_name = os.getenv("POSTGRES_DB", "navguard_db")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}"

def get_engine() -> Engine:
    """Create and return a SQLAlchemy engine."""
    return create_engine(get_db_url(), pool_pre_ping=True)
