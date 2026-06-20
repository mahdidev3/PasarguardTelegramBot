
# Migrations

Phase 1 introduces SQLAlchemy models and PostgreSQL storage. For this checkpoint
`app.bootstrap.bootstrap_phase1()` calls `Base.metadata.create_all()` so a fresh
PostgreSQL database can be tested quickly.

The next database-hardening pass should switch startup creation to Alembic-only
migrations. The full schema is defined in `app/models.py` and can be converted to
Alembic revisions with:

```bash
alembic revision --autogenerate -m "phase1 schema"
alembic upgrade head
```






