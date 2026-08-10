"""Database configuration.

Local development uses SQLite. On Azure, set DATABASE_URL to an Azure SQL
connection string, e.g.:
  mssql+pyodbc://user:pass@server.database.windows.net/vocdb?driver=ODBC+Driver+18+for+SQL+Server
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./voc_tracker.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
