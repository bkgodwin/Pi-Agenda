from __future__ import annotations

import ipaddress
import secrets
import socket
import sqlite3
import time
from functools import wraps
from urllib.parse import urljoin, urlsplit

import requests
from flask import abort, current_app, request, session

_LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def is_loopback_request() -> bool:
    try:
        return ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def player_access_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_loopback_request() or session.get("authenticated"):
            return view(*args, **kwargs)
        remote_enabled = current_app.config.get("REMOTE_PLAYER_ENABLED")
        try:
            from .db import get_db, get_setting

            remote_enabled = get_setting(get_db(), "remote_player_enabled", "0") == "1"
        except (RuntimeError, sqlite3.Error):
            remote_enabled = False
        if remote_enabled:
            expected = current_app.config.get("PLAYER_TOKEN", "")
            supplied = request.cookies.get("pi_agenda_player") or request.headers.get(
                "X-Player-Token", ""
            )
            if expected and secrets.compare_digest(supplied, expected):
                return view(*args, **kwargs)
        abort(403)

    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            from flask import redirect, url_for

            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def api_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            from flask import jsonify

            return jsonify(
                ok=False,
                data=None,
                error={"code": "auth_required", "message": "Authentication required"},
            ), 401
        return view(*args, **kwargs)

    return wrapped


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf() -> None:
    expected = session.get("csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    if not expected or not secrets.compare_digest(str(expected), str(supplied)):
        abort(400, "Invalid CSRF token")


def rate_limit_login(address: str, limit: int = 8, window: int = 300) -> bool:
    now = time.monotonic()
    attempts = [
        stamp for stamp in _LOGIN_ATTEMPTS.get(address, []) if now - stamp < window
    ]
    _LOGIN_ATTEMPTS[address] = attempts
    return len(attempts) < limit


def record_login_failure(address: str) -> None:
    _LOGIN_ATTEMPTS.setdefault(address, []).append(time.monotonic())


def clear_login_failures(address: str) -> None:
    _LOGIN_ATTEMPTS.pop(address, None)


def _host_allowed(hostname: str, allowlist: set[str]) -> bool:
    if hostname.lower() in allowlist:
        return True
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as exc:
        raise ValueError("Hostname could not be resolved") from exc
    if not addresses:
        raise ValueError("Hostname did not resolve")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(
                "Private, loopback, link-local, and reserved targets are blocked"
            )
    return True


def validate_remote_url(url: str, allowlist: set[str] | None = None) -> str:
    allowlist = {item.lower() for item in (allowlist or set())}
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("Only HTTP and HTTPS URLs are supported")
    if not parts.hostname or parts.username or parts.password:
        raise ValueError("URL must have a hostname and cannot contain credentials")
    _host_allowed(parts.hostname, allowlist)
    return url


def safe_get(
    url: str,
    *,
    allowlist: set[str] | None = None,
    max_bytes: int,
    timeout: tuple[int, int] = (5, 20),
    max_redirects: int = 5,
) -> tuple[bytes, str, dict[str, str]]:
    current = validate_remote_url(url, allowlist)
    for _ in range(max_redirects + 1):
        with requests.get(
            current,
            stream=True,
            timeout=timeout,
            allow_redirects=False,
            headers={"User-Agent": "Pi-Agenda/0.1"},
        ) as response:
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Remote server returned an empty redirect")
                current = validate_remote_url(urljoin(current, location), allowlist)
                continue
            response.raise_for_status()
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared > max_bytes:
                raise ValueError("Remote response exceeds the configured size limit")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        "Remote response exceeds the configured size limit"
                    )
                chunks.append(chunk)
            headers = {key.lower(): value for key, value in response.headers.items()}
            return b"".join(chunks), current, headers
    raise ValueError("Remote URL redirected too many times")
