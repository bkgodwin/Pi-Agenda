from __future__ import annotations

import argparse
import getpass
import http.server
import logging
import os
import socketserver
import sys
from pathlib import Path

import bcrypt
from waitress import serve

from . import __version__
from .app import create_app
from .backup import restore_backup
from .config import RuntimeConfig
from .db import connect, get_setting, migrate, set_setting
from .worker import run_worker


class CacheRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; form-action 'none'; frame-ancestors http://127.0.0.1:*",
        )
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        logging.getLogger("pi_agenda.cache").info(format, *args)


def set_password(config: RuntimeConfig, supplied: str | None = None) -> None:
    password = supplied
    if password is None:
        password = getpass.getpass("Management password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise SystemExit("Passwords do not match")
    if len(password) < 8:
        raise SystemExit("Password must contain at least eight characters")
    migrate(config.db_path)
    conn = connect(config.db_path)
    try:
        set_setting(
            conn,
            "password_hash",
            bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        )
    finally:
        conn.close()
    print("Management password updated.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pi-agenda")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db")
    password_parser = subparsers.add_parser("set-password")
    password_parser.add_argument("--password-stdin", action="store_true")
    subparsers.add_parser("run-web")
    subparsers.add_parser("run-worker")
    subparsers.add_parser("run-cache")
    subparsers.add_parser("status")
    restore_parser = subparsers.add_parser("restore-backup")
    restore_parser.add_argument("archive")
    args = parser.parse_args(argv)
    config = RuntimeConfig.from_env()
    config.ensure_directories()

    if args.command == "init-db":
        migrate(config.db_path)
        print(f"Database ready: {config.db_path}")
    elif args.command == "set-password":
        supplied = sys.stdin.readline().rstrip("\n") if args.password_stdin else None
        set_password(config, supplied)
    elif args.command == "run-web":
        logging.basicConfig(level=os.environ.get("PI_AGENDA_LOG_LEVEL", "INFO"))
        app = create_app(config)
        threads = int(os.environ.get("PI_AGENDA_WEB_THREADS", "4"))
        serve(app, host=config.host, port=config.port, threads=threads)
    elif args.command == "run-worker":
        logging.basicConfig(level=os.environ.get("PI_AGENDA_LOG_LEVEL", "INFO"))
        run_worker(config)
    elif args.command == "run-cache":
        logging.basicConfig(level=os.environ.get("PI_AGENDA_LOG_LEVEL", "INFO"))
        directory = config.data_dir / "generations"
        handler = lambda *values, **kwargs: CacheRequestHandler(
            *values, directory=str(directory), **kwargs
        )
        with socketserver.ThreadingTCPServer(
            ("127.0.0.1", config.cache_port), handler
        ) as server:
            server.daemon_threads = True
            server.serve_forever()
    elif args.command == "status":
        migrate(config.db_path)
        conn = connect(config.db_path)
        try:
            password_set = bool(get_setting(conn, "password_hash", ""))
            items = conn.execute("SELECT COUNT(*) count FROM media_items").fetchone()[
                "count"
            ]
            jobs = conn.execute(
                "SELECT COUNT(*) count FROM jobs WHERE state IN ('queued','running')"
            ).fetchone()["count"]
            print(f"Pi-Agenda {__version__}")
            print(f"Data: {config.data_dir}")
            print(f"Port: {config.port}")
            print(f"Password configured: {'yes' if password_set else 'no'}")
            print(f"Items: {items}; active jobs: {jobs}")
        finally:
            conn.close()
    elif args.command == "restore-backup":
        safety = restore_backup(config, Path(args.archive).resolve())
        print(f"Backup restored. Pre-restore safety copy: {safety}")


if __name__ == "__main__":
    main()
