from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from .config import RuntimeConfig
from .db import SCHEMA_VERSION, backup_database, migrate


class BackupError(RuntimeError):
    pass


def validate_backup(archive: Path, *, max_bytes: int) -> None:
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise BackupError("Backup is not a valid ZIP file")
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        if "db.sqlite" not in names or "manifest.json" not in names:
            raise BackupError("Backup must contain db.sqlite and manifest.json")
        total = 0
        for member in bundle.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise BackupError("Backup contains an unsafe path")
            if path.parts and path.parts[0] not in {
                "db.sqlite",
                "manifest.json",
                "uploads",
                "generations",
            }:
                raise BackupError("Backup contains an unexpected path")
            total += member.file_size
            if total > max_bytes:
                raise BackupError("Backup exceeds the configured content quota")
        try:
            manifest = json.loads(bundle.read("manifest.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise BackupError("Backup manifest is invalid") from exc
        if int(manifest.get("schema", 0)) > SCHEMA_VERSION:
            raise BackupError("Backup was created by a newer Pi-Agenda version")


def _pre_restore_backup(config: RuntimeConfig) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = config.data_dir / "backups" / f"pre-restore-{stamp}.zip"
    with tempfile.TemporaryDirectory(dir=config.data_dir / "staging") as temp:
        snapshot = Path(temp) / "db.sqlite"
        backup_database(config.db_path, snapshot)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle:
            bundle.write(snapshot, "db.sqlite")
            bundle.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "schema": SCHEMA_VERSION,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                ),
            )
            uploads = config.data_dir / "uploads"
            paths = uploads.rglob("*") if uploads.exists() else []
            for path in paths:
                if path.is_file():
                    bundle.write(path, Path("uploads") / path.relative_to(uploads))
            conn = sqlite3.connect(snapshot)
            try:
                active_paths = [
                    row[0]
                    for row in conn.execute(
                        """SELECT g.relative_path FROM media_items m
                           JOIN media_generations g ON g.id = m.active_generation_id
                           WHERE m.deleted_at IS NULL"""
                    )
                ]
            finally:
                conn.close()
            generation_root = config.data_dir / "generations"
            for relative in active_paths:
                generation_path = (config.data_dir / relative).resolve()
                if generation_root.resolve() not in generation_path.parents:
                    continue
                for path in generation_path.rglob("*"):
                    if path.is_file():
                        bundle.write(
                            path,
                            Path("generations") / path.relative_to(generation_root),
                        )
    return destination


def restore_backup(config: RuntimeConfig, archive: Path) -> Path:
    quota = 20 * 1024 * 1024 * 1024
    if config.db_path.exists():
        conn = sqlite3.connect(config.db_path)
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'content_quota_bytes'"
            ).fetchone()
            if row:
                quota = int(row[0])
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    validate_backup(archive, max_bytes=quota)
    safety_backup = _pre_restore_backup(config)
    restore_root = Path(
        tempfile.mkdtemp(prefix="restore-", dir=config.data_dir / "staging")
    )
    old_uploads = config.data_dir / "staging" / "uploads-before-restore"
    old_generations = config.data_dir / "staging" / "generations-before-restore"
    try:
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(restore_root)
        restored_db = restore_root / "db.sqlite"
        check = sqlite3.connect(restored_db)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise BackupError("Restored database failed its integrity check")
        finally:
            check.close()
        migrate(restored_db)

        restored_uploads = restore_root / "uploads"
        restored_generations = restore_root / "generations"
        current_uploads = config.data_dir / "uploads"
        current_generations = config.data_dir / "generations"
        if old_uploads.exists():
            shutil.rmtree(old_uploads)
        if current_uploads.exists():
            os.replace(current_uploads, old_uploads)
        if restored_uploads.exists():
            os.replace(restored_uploads, current_uploads)
        else:
            current_uploads.mkdir(parents=True)
        if old_generations.exists():
            shutil.rmtree(old_generations)
        if current_generations.exists():
            os.replace(current_generations, old_generations)
        if restored_generations.exists():
            os.replace(restored_generations, current_generations)
        else:
            current_generations.mkdir(parents=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{config.db_path}{suffix}").unlink(missing_ok=True)
        os.replace(restored_db, config.db_path)
        shutil.rmtree(old_uploads, ignore_errors=True)
        shutil.rmtree(old_generations, ignore_errors=True)
        archive.unlink(missing_ok=True)
        return safety_backup
    except Exception:
        if old_uploads.exists():
            shutil.rmtree(config.data_dir / "uploads", ignore_errors=True)
            os.replace(old_uploads, config.data_dir / "uploads")
        if old_generations.exists():
            shutil.rmtree(config.data_dir / "generations", ignore_errors=True)
            os.replace(old_generations, config.data_dir / "generations")
        raise
    finally:
        shutil.rmtree(restore_root, ignore_errors=True)
