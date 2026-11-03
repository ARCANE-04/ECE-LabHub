import getpass
import os
import secrets
from zoneinfo import ZoneInfo

import click
from flask import Flask, render_template, request, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError

from .extensions import csrf, db, login_manager
from . import models


def _secret_key(app):
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(app.instance_path, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as fh:
        return fh.read().strip()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)
    env = os.environ.get
    app.config.update(
        SQLALCHEMY_DATABASE_URI=env("DATABASE_URL", "sqlite:///" + os.path.join(app.instance_path, "labportal.db")),
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 15}},
        TIMEZONE=env("PORTAL_TIMEZONE", "Asia/Kolkata"),
        STUDENT_EMAIL_DOMAINS=[d.strip().lower() for d in env("STUDENT_EMAIL_DOMAINS", "iitr.ac.in").split(",")],
        STAFF_EMAIL_DOMAINS=[d.strip().lower() for d in env("STAFF_EMAIL_DOMAINS", "iitr.ac.in").split(",")],
        VERIFY_TOKEN_HOURS=24,
        RESET_TOKEN_MINUTES=30,
        BOOKING_MAX_DAYS_AHEAD=30,
        BOOKING_MAX_HOURS=4,
        BOOKING_MIN_MINUTES=30,
        BOOKING_MAX_UNITS=int(env("BOOKING_MAX_UNITS", "5")),
        BOOKING_DAY_START=env("BOOKING_DAY_START", "09:00"),
        BOOKING_DAY_END=env("BOOKING_DAY_END", "17:30"),
        # No-booking breaks inside the day, "HH:MM-HH:MM" comma-separated (lunch by default).
        BOOKING_BREAKS=[b.strip() for b in env("BOOKING_BREAKS", "13:00-14:00").split(",") if b.strip()],
        # Weekdays on which students may book (0 = Monday ... 6 = Sunday). Default Mon-Fri.
        BOOKING_WEEKDAYS=[int(d) for d in env("BOOKING_WEEKDAYS", "0,1,2,3,4").split(",") if d.strip()],
        MAIL_SERVER=env("MAIL_SERVER"),
        MAIL_PORT=int(env("MAIL_PORT", "587")),
        MAIL_USERNAME=env("MAIL_USERNAME"),
        MAIL_PASSWORD=env("MAIL_PASSWORD"),
        MAIL_USE_TLS=env("MAIL_USE_TLS", "1") == "1",
        MAIL_USE_SSL=env("MAIL_USE_SSL", "0") == "1",
        MAIL_SENDER=env("MAIL_SENDER", "IITR Lab Equipment Portal <no-reply@localhost>"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=env("SESSION_COOKIE_SECURE", "0") == "1",
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        WTF_CSRF_TIME_LIMIT=None,
    )
    if test_config:
        app.config.update(test_config)
    app.config.setdefault("SECRET_KEY", None)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = _secret_key(app)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.session_protection = "strong"

    from .auth import staff_auth, student_auth
    from .main import main
    from .staff import staff
    app.register_blueprint(main)
    app.register_blueprint(student_auth)
    app.register_blueprint(staff_auth)
    app.register_blueprint(staff)

    with app.app_context():
        _check_schema(app)
        db.create_all()
        if db.engine.url.get_backend_name() == "sqlite" and db.engine.url.database not in (None, "", ":memory:"):
            with db.engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        if not app.config.get("SKIP_SEED"):
            from .seed_data import seed
            seed(db, models)

    _register_template_helpers(app)
    _register_errors(app)
    _register_cli(app)

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return resp

    return app


def _check_schema(app):
    """Refuse to start on a database created by an older version (missing unit-level columns)."""
    from sqlalchemy import inspect
    insp = inspect(db.engine)
    if "equipment" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("equipment")}
        if "units_functional" not in cols:
            raise RuntimeError(
                "This database was created by an earlier version of the portal. Back it up and run "
                "`flask --app portal reset-data`, or delete instance/labportal.db, then start again.")


def _register_template_helpers(app):
    tz = ZoneInfo(app.config["TIMEZONE"])

    @app.template_filter("localdt")
    def localdt(value, fmt="%d %b %Y, %H:%M"):
        if not value:
            return "—"
        from datetime import timezone
        return value.replace(tzinfo=timezone.utc).astimezone(tz).strftime(fmt)

    @app.template_filter("cond_slug")
    def cond_slug(value):
        return (value or "unknown").lower().replace(" ", "-")

    from .seed_data import CATEGORY_IMAGE
    from .services import unit_summary

    @app.context_processor
    def inject():
        def static_or_url(path):
            if not path:
                return url_for("static", filename="img/equipment/fallback.svg")
            return path if path.startswith("https://") else url_for("static", filename=path)

        logo_file = os.path.join(app.static_folder, "img", "iitr-logo.png")
        return {
            "is_staff": current_user.is_authenticated and current_user.role == "staff",
            "is_student": current_user.is_authenticated and current_user.role == "student",
            "img_src": static_or_url,
            "iitr_logo_available": os.path.exists(logo_file),
            "cat_images": CATEGORY_IMAGE,
            "unit_summary": unit_summary,
            "UNIT_CONDITIONS": ["Functional", "Partially Functional", "Damaged", "Unknown"],
        }


def _register_errors(app):
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403, title="Access denied",
                               message="Your account does not have permission to view this page."), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, title="Page not found",
                               message="The page you were looking for does not exist."), 404

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        return render_template("error.html", code=400, title="Form expired",
                               message="Your form session expired. Go back, refresh the page and try again."), 400


def _register_cli(app):
    @app.cli.command("create-staff")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt="Full name")
    @click.option("--password", default=None, help="Omit to be prompted (recommended).")
    @click.option("--admin/--no-admin", default=True, help="Administrators can issue staff access codes.")
    def create_staff(email, name, password, admin):
        """Create (or reset) a staff account. Staff cannot self-register."""
        from .auth import normalise_email, password_problem
        from .models import User, utcnow
        email_n = normalise_email(email)
        if not email_n:
            raise click.ClickException("Invalid email address.")
        if password is None:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Repeat password: "):
                raise click.ClickException("Passwords do not match.")
        problem = password_problem(password)
        if problem:
            raise click.ClickException(problem)
        user = models.User.query.filter_by(email=email_n).first()
        if user and user.role != "staff":
            raise click.ClickException("That email belongs to a student account.")
        if not user:
            user = models.User(email=email_n, role="staff", full_name=name, email_verified_at=utcnow())
            db.session.add(user)
        user.full_name = name
        user.is_admin = admin
        user.set_password(password)
        db.session.commit()
        click.echo(f"Staff account ready: {email_n}" + (" (administrator)" if admin else ""))

    @app.cli.command("create-staff-code")
    @click.option("--label", default="", help="Who the code is for, e.g. 'Microwave Lab technicians'.")
    @click.option("--uses", default=1, show_default=True, help="How many accounts may be created with it.")
    @click.option("--days", default=7, show_default=True, help="Days until the code expires.")
    def create_staff_code(label, uses, days):
        """Issue a staff sign-up access code (shown once)."""
        from .auth import generate_access_code
        plain, code = generate_access_code(label, uses, days)
        click.echo(f"Staff access code: {plain}  (uses: {code.max_uses}, expires: {code.expires_at:%Y-%m-%d %H:%M} UTC)")

    @app.cli.command("reset-data")
    @click.confirmation_option(prompt="This deletes ALL data (users, bookings, issues) and re-imports the inventory. Continue?")
    def reset_data():
        """Drop everything and re-import labs + equipment from data/source."""
        from .seed_data import seed
        db.drop_all()
        db.create_all()
        seed(db, models)
        click.echo("Database rebuilt from source inventories.")

    @app.cli.command("stats")
    def stats():
        """Print inventory statistics computed from the database."""
        from .services import inventory_stats, lab_breakdown
        s = inventory_stats()
        click.echo(f"Records: {s['records']}  Units: {s['quantity']}")
        for c, v in s["by_condition"].items():
            click.echo(f"  {c:22s} records={v['records']:4d} units={v['quantity']:5d}")
        for row in lab_breakdown():
            click.echo(f"{row['lab'].name:28s} records={row['records']:4d} units={row['quantity']:5d}")
