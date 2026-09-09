from __future__ import annotations

import bcrypt
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .db import get_db, get_setting
from .security import (
    clear_login_failures,
    csrf_token,
    rate_limit_login,
    record_login_failure,
    validate_csrf,
)

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        validate_csrf()
        address = request.remote_addr or "unknown"
        if not rate_limit_login(address):
            flash("Too many login attempts. Wait a few minutes and try again.", "error")
            return render_template("login.html"), 429
        password = request.form.get("password", "")
        stored = get_setting(get_db(), "password_hash", "")
        valid = False
        if stored:
            try:
                valid = bcrypt.checkpw(password.encode(), stored.encode())
            except ValueError:
                valid = False
        if valid:
            clear_login_failures(address)
            session.clear()
            session["authenticated"] = True
            session.permanent = True
            csrf_token()
            target = request.args.get("next", "")
            if not target.startswith("/") or target.startswith("//"):
                target = url_for("admin.dashboard")
            return redirect(target)
        record_login_failure(address)
        flash("Incorrect password.", "error")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("auth.login"))
