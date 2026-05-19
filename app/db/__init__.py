"""Database package — async SQLAlchemy engine, session, and models.

Everything DB-related goes through :mod:`app.db.session` (the async
engine + ``get_db`` FastAPI dependency).  Models live in
:mod:`app.db.models` and are re-exported there for Alembic.
"""
