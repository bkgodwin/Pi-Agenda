from __future__ import annotations

import json
import socket
import zipfile

import pytest

from pi_agenda.backup import BackupError, validate_backup
from pi_agenda.security import validate_remote_url


def test_backup_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"schema": 1}))
        bundle.writestr("db.sqlite", b"not important for structure validation")
        bundle.writestr("../escape", b"bad")
    with pytest.raises(BackupError):
        validate_backup(archive, max_bytes=1024 * 1024)


def test_url_validation_blocks_private_address(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
        ],
    )
    with pytest.raises(ValueError, match="blocked"):
        validate_remote_url("http://example.test/page")
    assert validate_remote_url("http://example.test/page", {"example.test"})
