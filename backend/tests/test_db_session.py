from app.db.session import _coerce_sync_database_url


def test_coerce_sync_database_url_maps_asyncpg_to_psycopg():
    assert _coerce_sync_database_url("postgresql+asyncpg://user:pass@db:5432/autocve") == (
        "postgresql+psycopg://user:pass@db:5432/autocve"
    )
