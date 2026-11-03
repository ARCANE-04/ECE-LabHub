"""Authentication for students and staff.

* Students register with an IIT Roorkee institutional address and prove they own it via a
  signed, time-limited, single-use link before they can sign in.
* Staff register with an institutional address PLUS a staff access code issued by a portal
  administrator (only a SHA-256 digest of each code is stored; codes expire, have a use limit and
  can be revoked). Staff also verify their email before signing in.
* The first administrator is created with `flask --app portal create-staff --admin`.
"""
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

from email_validator import EmailNotValidError, validate_email
from flask import (Blueprint, abort, current_app, flash, redirect, render_template, request,
                   url_for)
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import update

from .extensions import db, login_manager
from .mailer import send_mail
from .models import StaffAccessCode, User, utcnow

student_auth = Blueprint("student_auth", __name__)
staff_auth = Blueprint("staff_auth", __name__, url_prefix="/staff")

MAX_FAILED = 5
LOCK_MINUTES = 15
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O/1/I confusion


# ------------------------------------------------------------------ session plumbing
@login_manager.user_loader
def load_user(session_id):
    try:
        uid, version = session_id.split(":")
        user = db.session.get(User, int(uid))
    except (ValueError, TypeError):
        return None
    if user and str(user.token_version) == version and user.is_enabled:
        return user
    return None


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/staff"):
        flash("Please sign in with your staff account.", "warning")
        return redirect(url_for("staff_auth.login", next=request.full_path))
    if request.path.startswith("/api/"):
        return {"error": "Sign in required"}, 401
    flash("Please sign in to continue.", "info")
    return redirect(url_for("student_auth.login", next=request.full_path))


def student_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "student":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def staff_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "staff":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @staff_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# ------------------------------------------------------------------ helpers
def _serializer(purpose):
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=f"labportal-{purpose}")


def make_token(user, purpose):
    return _serializer(purpose).dumps({"uid": user.id, "email": user.email, "v": user.token_version})


def read_token(token, purpose, max_age):
    try:
        data = _serializer(purpose).loads(token, max_age=max_age)
    except SignatureExpired:
        return None, "expired"
    except BadSignature:
        return None, "invalid"
    user = db.session.get(User, data.get("uid"))
    if not user or user.email != data.get("email") or user.token_version != data.get("v"):
        return None, "invalid"
    return user, None


def _domain_ok(email, domains):
    domain = email.rsplit("@", 1)[-1].lower()
    return any(domain == d or domain.endswith("." + d) for d in domains)


def is_institutional(email, role="student"):
    key = "STUDENT_EMAIL_DOMAINS" if role == "student" else "STAFF_EMAIL_DOMAINS"
    return _domain_ok(email, current_app.config[key])


def normalise_email(raw):
    try:
        return validate_email((raw or "").strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def password_problem(pw):
    if len(pw) < 10:
        return "Password must be at least 10 characters."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "Password must contain both letters and numbers."
    return None


def safe_next(default):
    target = request.args.get("next") or request.form.get("next")
    if target and not urlparse(target).netloc and target.startswith("/") and not target.startswith("//"):
        return target
    return default


def home_for(user):
    return url_for("staff.dashboard") if user.role == "staff" else url_for("main.home")


def send_verification(user):
    link = url_for("student_auth.verify", token=make_token(user, "verify"), _external=True)
    hours = current_app.config["VERIFY_TOKEN_HOURS"]
    who = "staff" if user.role == "staff" else "student"
    send_mail(user.email, "Verify your IIT Roorkee Lab Equipment Portal account",
              f"Hello {user.full_name},\n\nConfirm your institutional email address to activate your "
              f"{who} account on the Lab Equipment Portal:\n\n{link}\n\nThis link expires in {hours} hours "
              f"and can be used once. If you did not register, ignore this message.\n")


def _attempt_login(role, email, password):
    """Shared credential check with lockout. Returns (user, error_message)."""
    generic = "Incorrect email or password."
    user = User.query.filter_by(email=email).first() if email else None
    if not user or user.role != role:
        return None, generic
    now = utcnow()
    if user.locked_until and user.locked_until > now:
        mins = int((user.locked_until - now).total_seconds() // 60) + 1
        return None, f"Too many failed attempts. Try again in {mins} minute(s)."
    if not user.check_password(password):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED:
            user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
            user.failed_logins = 0
        db.session.commit()
        return None, generic
    if not user.is_enabled:
        return None, "This account has been disabled. Contact the portal administrator."
    if not user.is_verified:
        return None, "unverified"
    user.failed_logins = 0
    user.locked_until = None
    return user, None


# ------------------------------------------------------------------ staff access codes
def _digest(code):
    clean = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    return hmac.new(current_app.config["SECRET_KEY"].encode(), clean.encode(), hashlib.sha256).hexdigest(), clean


def generate_access_code(label, max_uses, days, created_by=None):
    """Create a code; returns (plain_code, StaffAccessCode). The plain code is never stored."""
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    plain = "-".join(raw[i:i + 4] for i in range(0, 12, 4))
    digest, _ = _digest(plain)
    code = StaffAccessCode(code_hash=digest, hint=raw[-4:], label=(label or "").strip()[:160] or None,
                           max_uses=max(1, int(max_uses)), expires_at=utcnow() + timedelta(days=max(1, int(days))),
                           created_by_id=created_by.id if created_by else None)
    db.session.add(code)
    db.session.commit()
    return plain, code


def find_valid_code(plain):
    digest, clean = _digest(plain)
    if len(clean) != 12:
        return None
    code = StaffAccessCode.query.filter_by(code_hash=digest).first()
    return code if code and code.state == "Active" else None


def consume_code(code):
    """Atomically use one slot of the code; False if it was used up concurrently."""
    res = db.session.execute(update(StaffAccessCode)
                             .where(StaffAccessCode.id == code.id, StaffAccessCode.revoked.is_(False),
                                    StaffAccessCode.uses < StaffAccessCode.max_uses,
                                    StaffAccessCode.expires_at > utcnow())
                             .values(uses=StaffAccessCode.uses + 1))
    return res.rowcount == 1


# ------------------------------------------------------------------ shared sign-up form
def _registration(role):
    fields = ["full_name", "email", "enrollment_no", "department"] if role == "student" else \
             ["full_name", "email", "designation", "department", "access_code"]
    form = {k: (request.form.get(k) or "").strip() for k in fields}
    pw, pw2 = request.form.get("password") or "", request.form.get("password2") or ""
    errors = {}
    email = normalise_email(form["email"])
    if len(form["full_name"]) < 2:
        errors["full_name"] = "Enter your full name."
    if not email:
        errors["email"] = "Enter a valid email address."
    elif not is_institutional(email, role):
        errors["email"] = "Use your IIT Roorkee institutional email address (…@iitr.ac.in)."
    if role == "student" and not re.fullmatch(r"[A-Za-z0-9/-]{4,20}", form["enrollment_no"]):
        errors["enrollment_no"] = "Enter your enrollment number."
    if role == "staff" and len(form["designation"]) < 2:
        errors["designation"] = "Enter your designation (e.g. Lab Technician)."
    code = None
    if role == "staff":
        code = find_valid_code(form["access_code"])
        if not code:
            errors["access_code"] = "This access code is invalid, expired or already used."
    problem = password_problem(pw)
    if problem:
        errors["password"] = problem
    elif pw != pw2:
        errors["password2"] = "Passwords do not match."
    return form, errors, email, pw, code


# ------------------------------------------------------------------ student routes
@student_auth.route("/student/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    form, errors = {"full_name": "", "email": "", "enrollment_no": "", "department": ""}, {}
    if request.method == "POST":
        form, errors, email, pw, _ = _registration("student")
        if not errors:
            existing = User.query.filter_by(email=email).first()
            if existing:
                if not existing.is_verified:
                    send_verification(existing)   # resend; never reveal account state
            else:
                user = User(email=email, full_name=form["full_name"], role="student",
                            enrollment_no=form["enrollment_no"].upper(), department=form["department"] or None)
                user.set_password(pw)
                db.session.add(user)
                db.session.commit()
                send_verification(user)
            return render_template("auth/check_email.html", email=email, purpose="verify", role="student")
        flash("Please correct the highlighted fields.", "danger")
    return render_template("auth/register.html", role="student", form=form, errors=errors)


@student_auth.route("/student/login", methods=["GET", "POST"])
def login():
    return _login("student")


@student_auth.route("/logout", methods=["POST"])
@login_required
def logout():
    role = current_user.role
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("staff_auth.login" if role == "staff" else "main.home"))


@student_auth.route("/account/verify/<token>")
def verify(token):
    user, err = read_token(token, "verify", current_app.config["VERIFY_TOKEN_HOURS"] * 3600)
    if err == "expired":
        flash("This verification link has expired. Request a new one below.", "danger")
        return redirect(url_for("student_auth.resend"))
    if err:
        flash("This verification link is invalid or has already been used.", "danger")
        return redirect(url_for("main.home"))
    if not user.is_verified:
        user.email_verified_at = utcnow()
        user.token_version += 1   # single use
        db.session.commit()
    flash("Email verified. You can now sign in.", "success")
    return redirect(url_for("staff_auth.login" if user.role == "staff" else "student_auth.login"))


@student_auth.route("/account/resend-verification", methods=["GET", "POST"])
def resend():
    if request.method == "POST":
        email = normalise_email(request.form.get("email"))
        if email:
            user = User.query.filter_by(email=email).first()
            if user and not user.is_verified:
                send_verification(user)
        return render_template("auth/check_email.html", email=email or "", purpose="verify",
                               role=request.form.get("role", "student"))
    return render_template("auth/simple_email_form.html", title="Resend verification email",
                           action=url_for("student_auth.resend"), button="Send verification link",
                           role=request.args.get("role", "student"))


@student_auth.route("/account/forgot-password", methods=["GET", "POST"])
def forgot():
    role = request.values.get("role", "student")
    if request.method == "POST":
        email = normalise_email(request.form.get("email"))
        if email:
            user = User.query.filter_by(email=email).first()
            if user and user.is_enabled:
                link = url_for("student_auth.reset", token=make_token(user, "reset"), _external=True)
                mins = current_app.config["RESET_TOKEN_MINUTES"]
                send_mail(user.email, "Reset your Lab Equipment Portal password",
                          f"Hello {user.full_name},\n\nUse this link to set a new password:\n\n{link}\n\n"
                          f"It expires in {mins} minutes and works once. If you did not request a reset, "
                          f"ignore this message.\n")
        return render_template("auth/check_email.html", email=email or "", purpose="reset", role=role)
    return render_template("auth/simple_email_form.html", title="Reset your password",
                           action=url_for("student_auth.forgot", role=role), button="Send reset link", role=role)


@student_auth.route("/account/reset-password/<token>", methods=["GET", "POST"])
def reset(token):
    user, err = read_token(token, "reset", current_app.config["RESET_TOKEN_MINUTES"] * 60)
    if err:
        flash("This reset link is invalid, expired or already used.", "danger")
        return redirect(url_for("student_auth.forgot"))
    error = None
    if request.method == "POST":
        pw, pw2 = request.form.get("password") or "", request.form.get("password2") or ""
        error = password_problem(pw) or (None if pw == pw2 else "Passwords do not match.")
        if not error:
            user.set_password(pw)   # bumps token_version: single-use link, other sessions end
            if not user.is_verified:
                user.email_verified_at = utcnow()   # receiving the mail proves mailbox ownership
            user.failed_logins, user.locked_until = 0, None
            db.session.commit()
            flash("Password updated. Please sign in.", "success")
            return redirect(url_for("staff_auth.login" if user.role == "staff" else "student_auth.login"))
    return render_template("auth/reset_password.html", error=error, role=user.role)


# ------------------------------------------------------------------ staff routes
@staff_auth.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(home_for(current_user))
    form = {"full_name": "", "email": "", "designation": "", "department": "",
            "access_code": request.args.get("code", "")}
    errors = {}
    if request.method == "POST":
        form, errors, email, pw, code = _registration("staff")
        if not errors and User.query.filter_by(email=email).first():
            # do not consume a code for an address that already has an account
            existing = User.query.filter_by(email=email).first()
            if not existing.is_verified:
                send_verification(existing)
            return render_template("auth/check_email.html", email=email, purpose="verify", role="staff")
        if not errors:
            if not consume_code(code):
                errors["access_code"] = "This access code was just used up. Ask your administrator for a new one."
            else:
                user = User(email=email, full_name=form["full_name"], role="staff", designation=form["designation"],
                            department=form["department"] or None, joined_with_code_id=code.id)
                user.set_password(pw)
                db.session.add(user)
                db.session.commit()
                send_verification(user)
                return render_template("auth/check_email.html", email=email, purpose="verify", role="staff")
        db.session.rollback()
        form["access_code"] = ""   # never echo a code back
        flash("Please correct the highlighted fields.", "danger")
    return render_template("auth/register.html", role="staff", form=form, errors=errors)


@staff_auth.route("/login", methods=["GET", "POST"])
def login():
    return _login("staff")


def _login(role):
    if current_user.is_authenticated:
        if current_user.role == role:
            return redirect(home_for(current_user))
        flash(f"You are signed in as {current_user.role}. Sign out first to use a {role} account.", "warning")
        return redirect(home_for(current_user))
    email_value, show_resend = "", False
    if request.method == "POST":
        email_value = (request.form.get("email") or "").strip()
        user, err = _attempt_login(role, normalise_email(email_value), request.form.get("password") or "")
        if err == "unverified":
            show_resend = True
            flash("Your email address is not verified yet. Check your inbox or resend the link.", "warning")
        elif err:
            flash(err, "danger")
        else:
            user.last_login_at = utcnow()
            db.session.commit()
            login_user(user, remember=bool(request.form.get("remember")))
            flash(f"Welcome back, {user.full_name.split()[0]}.", "success")
            nxt = safe_next(home_for(user))
            if role == "staff" and not nxt.startswith("/staff"):
                nxt = home_for(user)
            if role == "student" and nxt.startswith("/staff"):
                nxt = home_for(user)
            return redirect(nxt)
    return render_template("auth/login.html", role=role, email=email_value, show_resend=show_resend)
