from __future__ import annotations

from pathlib import Path

import bcrypt
import pytest

from pi_agenda import create_app
from pi_agenda.config import RuntimeConfig
from pi_agenda.db import connect, migrate, set_setting


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeConfig:
    data = tmp_path / "data"
    return RuntimeConfig(
        data_dir=data,
        db_path=data / "db.sqlite",
        secret_path=data / "secrets" / "flask-secret",
        host="127.0.0.1",
        port=8000,
        cache_port=8002,
        testing=True,
        max_upload_bytes=10 * 1024 * 1024,
        max_remote_bytes=10 * 1024 * 1024,
        max_archive_bytes=10 * 1024 * 1024,
    )


@pytest.fixture
def app(runtime: RuntimeConfig):
    app = create_app(runtime)
    conn = connect(runtime.db_path)
    set_setting(
        conn,
        "password_hash",
        bcrypt.hashpw(b"classroom-pass", bcrypt.gensalt()).decode(),
    )
    conn.close()
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def authenticated_client(client):
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["csrf_token"] = "test-csrf"
    return client


@pytest.fixture
def db(runtime):
    runtime.ensure_directories()
    migrate(runtime.db_path)
    conn = connect(runtime.db_path)
    try:
        yield conn
    finally:
        conn.close()
