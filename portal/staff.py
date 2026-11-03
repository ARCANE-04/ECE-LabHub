"""Staff console - every route requires a signed-in staff account (role-based access control)."""
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .auth import admin_required, generate_access_code, staff_required
from .extensions import db
from .models import (ACCESS_LEVELS, ACTIVITY_TYPES, CONDITIONS, DAYS, ISSUE_STATUSES, RECORD_STATUSES,
                     Booking, ConditionLog, Equipment, Issue, Keyword, Lab, StaffAccessCode,
                     TimetableEntry, User, utcnow)
from .seed_data import CATEGORY_IMAGE
from .services import (BookingError, cancel_booking, inventory_stats, lab_breakdown, lab_status,
                       now_local, overlaps, parse_time, peak_booked_quantity, rebalance_bookings,
                       refresh_booking_statuses, unit_summary)

staff = Blueprint("staff", __name__, url_prefix="/staff")


@staff.before_request
@staff_required
def _guard():
    """Applies staff_required to every route in this blueprint."""
    return None


@staff.context_processor
def _counts():
    return {"pending_count": Booking.query.filter_by(status="Pending").count(),
            "open_issue_count": Issue.query.filter(Issue.status != "Resolved").count(),
            "now_hour": now_local().hour}


def _int(name, default=None):
    try:
        return int(request.form.get(name, default))
    except (TypeError, ValueError):
        return None


def _future_peak(eq):
    """Highest number of units held at once by any upcoming active booking."""
    now = now_local()
    future = Booking.query.filter(Booking.equipment_id == eq.id, Booking.status.in_(("Pending", "Confirmed")),
                                  Booking.booking_date >= now.date()).all()
    return max([peak_booked_quantity(eq.id, b.booking_date, b.start_time, b.end_time) for b in future] or [0])


def apply_units(eq, functional, partial, damaged, quantity=None):
    """Validate and apply a unit-level condition change. Returns (cancelled_bookings, error)."""
    quantity = eq.quantity if quantity is None else quantity
    if None in (functional, partial, damaged, quantity):
        return 0, "Enter whole numbers for every unit count."
    if quantity < 0 or min(functional, partial, damaged) < 0:
        return 0, "Unit counts cannot be negative."
    if functional + partial + damaged > quantity:
        return 0, (f"Working + partial + damaged ({functional + partial + damaged}) is more than the "
                   f"{quantity} units in stock.")
    if quantity < eq.quantity:
        peak = _future_peak(eq)
        if quantity - damaged < peak:
            return 0, (f"Cannot reduce stock to {quantity}: up to {peak} unit(s) are booked at the same time "
                       f"in upcoming bookings. Cancel or move those bookings first.")
    before = unit_summary(eq) + f" (of {eq.quantity})"
    eq.quantity = quantity
    eq.set_units(functional, partial, damaged)
    after = unit_summary(eq) + f" (of {eq.quantity})"
    if before != after:
        db.session.add(ConditionLog(equipment_id=eq.id, old_condition=before, new_condition=after,
                                    changed_by_id=current_user.id))
    cancelled = rebalance_bookings(eq, "Cancelled: not enough working units after a lab staff condition update")
    return cancelled, None


# ----------------------------------------------------------------- dashboard
@staff.route("/")
def dashboard():
    refresh_booking_statuses()
    pending = Booking.query.filter_by(status="Pending").order_by(Booking.booking_date, Booking.start_time).limit(8).all()
    today = now_local().date()
    todays = (Booking.query.filter(Booking.booking_date == today, Booking.status.in_(("Pending", "Confirmed")))
              .order_by(Booking.start_time).all())
    open_issues = Issue.query.filter(Issue.status != "Resolved").order_by(Issue.created_at.desc()).limit(5).all()
    recent_changes = ConditionLog.query.order_by(ConditionLog.changed_at.desc()).limit(6).all()
    attention = (Equipment.query.filter(Equipment.units_damaged > 0)
                 .order_by(Equipment.units_damaged.desc()).limit(6).all())
    labs = Lab.query.order_by(Lab.name).all()
    return render_template("staff/dashboard.html", stats=inventory_stats(), breakdown=lab_breakdown(),
                           pending=pending, todays=todays, open_issues=open_issues, attention=attention,
                           recent_changes=recent_changes, lab_states={l.id: lab_status(l) for l in labs},
                           nav="dashboard")


# ----------------------------------------------------------------- inventory
@staff.route("/inventory")
def inventory():
    lab_id = request.args.get("lab", type=int)
    status = request.args.get("status") or None
    has = request.args.get("has") or None
    q = (request.args.get("q") or "").strip()
    query = Equipment.query.join(Lab)
    if lab_id:
        query = query.filter(Equipment.lab_id == lab_id)
    if status in RECORD_STATUSES:
        query = query.filter(Equipment.condition == status)
    if has == "damaged":
        query = query.filter(Equipment.units_damaged > 0)
    elif has == "unverified":
        query = query.filter(Equipment.quantity > Equipment.units_functional + Equipment.units_partial + Equipment.units_damaged)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Equipment.name.ilike(like), Equipment.manufacturer.ilike(like),
                                    Equipment.model.ilike(like), Equipment.category.ilike(like)))
    items = query.order_by(Lab.name, Equipment.id).all()
    shown = {"records": len(items), "quantity": sum(e.quantity for e in items),
             "damaged": sum(e.units_damaged for e in items), "unknown": sum(e.units_unknown for e in items)}
    return render_template("staff/inventory.html", items=items, labs=Lab.query.order_by(Lab.name).all(),
                           statuses=RECORD_STATUSES, sel_lab=lab_id, sel_status=status, sel_has=has, q=q,
                           stats=inventory_stats(lab_id), shown=shown, nav="inventory")


@staff.route("/inventory/<int:eq_id>/units", methods=["POST"])
def update_units(eq_id):
    eq = db.session.get(Equipment, eq_id) or abort(404)
    back = request.form.get("back") or url_for("staff.inventory")
    if request.form.get("mode") == "all":           # quick action: every unit -> one condition
        target = request.form.get("condition")
        if target not in CONDITIONS:
            abort(400)
        n = eq.quantity
        counts = {"Functional": (n, 0, 0), "Partially Functional": (0, n, 0), "Damaged": (0, 0, n),
                  "Unknown": (0, 0, 0)}[target]
        cancelled, err = apply_units(eq, *counts)
    else:
        cancelled, err = apply_units(eq, _int("functional"), _int("partial"), _int("damaged"),
                                     _int("quantity", eq.quantity))
    if err:
        db.session.rollback()
        flash(err, "danger")
    else:
        db.session.commit()
        msg = f"{eq.name}: {unit_summary(eq)}."
        if cancelled:
            msg += f" {cancelled} upcoming booking(s) no longer fit and were cancelled."
        flash(msg, "success")
    return redirect(back)


def _equipment_from_form(eq):
    f = request.form
    errors = []
    name = (f.get("name") or "").strip()
    if len(name) < 2:
        errors.append("Name is required.")
    lab = db.session.get(Lab, f.get("lab_id", type=int))
    if not lab:
        errors.append("Choose a laboratory.")
    category = (f.get("category") or "").strip()
    if not category:
        errors.append("Category is required.")
    access = f.get("access_level") if f.get("access_level") in ACCESS_LEVELS else "Not specified"
    img = (f.get("image_path") or "").strip()
    if img and not (img.startswith("https://") or re.fullmatch(r"img/[\w./-]+", img)):
        errors.append("Image must be an https:// URL or a static path like img/equipment/x.svg.")
    if errors:
        return errors
    eq.name, eq.lab, eq.category, eq.access_level = name, lab, category, access
    eq.manufacturer = (f.get("manufacturer") or "").strip() or None
    eq.model = (f.get("model") or "").strip() or None
    eq.specification = (f.get("specification") or "").strip() or None
    eq.notes = (f.get("notes") or "").strip() or None
    eq.bookable = f.get("bookable") == "1"
    eq.image_path = img or "img/equipment/" + CATEGORY_IMAGE.get(category, "fallback.svg")
    eq.image_kind = "photo" if img.startswith("https://") else "illustration"
    eq.image_credit = (f.get("image_credit") or "").strip() or None
    words = {w.strip().lower() for w in (f.get("keywords") or "").split(",") if w.strip()}
    eq.keywords = [Keyword.query.filter_by(word=w).first() or Keyword(word=w) for w in sorted(words)]
    return []


@staff.route("/inventory/<int:eq_id>/edit", methods=["GET", "POST"])
@staff.route("/inventory/new", methods=["GET", "POST"], defaults={"eq_id": None})
def edit_equipment(eq_id):
    eq = db.session.get(Equipment, eq_id) if eq_id else Equipment(quantity=1, bookable=True, units_functional=0,
                                                                    units_partial=0, units_damaged=0)
    if eq is None:
        abort(404)
    if request.method == "POST":
        with db.session.no_autoflush:
            errors = _equipment_from_form(eq)
            cancelled = 0
            if not errors:
                if not eq.id:
                    eq.quantity = 0
                    db.session.add(eq)
                    db.session.flush()
                cancelled, err = apply_units(eq, _int("functional"), _int("partial"), _int("damaged"), _int("quantity"))
                if err:
                    errors.append(err)
        if not errors:
            db.session.commit()
            flash(f"Saved “{eq.name}” — {unit_summary(eq)}." +
                  (f" {cancelled} upcoming booking(s) cancelled." if cancelled else ""), "success")
            return redirect(url_for("staff.inventory", lab=eq.lab_id) + f"#eq-{eq.id}")
        db.session.rollback()
        for e in errors:
            flash(e, "danger")
        eq = db.session.get(Equipment, eq_id) if eq_id else Equipment(quantity=1, bookable=True, units_functional=0,
                                                                        units_partial=0, units_damaged=0)
    categories = sorted(set(CATEGORY_IMAGE) | {c for (c,) in db.session.query(Equipment.category).distinct()})
    history = (ConditionLog.query.filter_by(equipment_id=eq.id).order_by(ConditionLog.changed_at.desc()).all()
               if eq.id else [])
    return render_template("staff/equipment_form.html", eq=eq, labs=Lab.query.order_by(Lab.name).all(),
                           access_levels=ACCESS_LEVELS, categories=categories, history=history, nav="inventory")


@staff.route("/inventory/<int:eq_id>/delete", methods=["POST"])
def delete_equipment(eq_id):
    eq = db.session.get(Equipment, eq_id) or abort(404)
    if Booking.query.filter(Booking.equipment_id == eq.id, Booking.status.in_(("Pending", "Confirmed"))).count():
        flash("This item has active bookings. Cancel them before deleting the record.", "danger")
        return redirect(url_for("staff.edit_equipment", eq_id=eq.id))
    name = eq.name
    db.session.delete(eq)
    db.session.commit()
    flash(f"Deleted “{name}”.", "success")
    return redirect(url_for("staff.inventory"))


# ----------------------------------------------------------------- labs
@staff.route("/labs", methods=["GET", "POST"])
def labs():
    if request.method == "POST":
        lab = db.session.get(Lab, request.form.get("lab_id", type=int)) or abort(404)
        name = (request.form.get("name") or "").strip()
        if len(name) < 2:
            flash("Laboratory name is required.", "danger")
        else:
            lab.name = name
            lab.room_number = (request.form.get("room_number") or "").strip() or None
            lab.building = (request.form.get("building") or "").strip() or None
            lab.description = (request.form.get("description") or "").strip() or None
            lab.booking_enabled = request.form.get("booking_enabled") == "1"
            db.session.commit()
            flash(f"Updated {lab.name}.", "success")
        return redirect(url_for("staff.labs"))
    return render_template("staff/labs.html", labs=Lab.query.order_by(Lab.name).all(),
                           breakdown={r["lab"].id: r for r in lab_breakdown()}, nav="labs")


# ----------------------------------------------------------------- bookings
@staff.route("/bookings")
def bookings():
    refresh_booking_statuses()
    status = request.args.get("status") or None
    q = Booking.query
    if status:
        q = q.filter(Booking.status == status)
    rows = q.order_by(Booking.booking_date.desc(), Booking.start_time.desc()).limit(300).all()
    return render_template("staff/bookings.html", rows=rows, sel_status=status, nav="bookings")


@staff.route("/bookings/<ref>/decide", methods=["POST"])
def decide_booking(ref):
    b = Booking.query.filter_by(reference=ref).first_or_404()
    action = request.form.get("action")
    back = request.form.get("back") or url_for("staff.dashboard")
    try:
        if action == "confirm":
            if b.status != "Pending":
                raise BookingError(f"{b.reference} is {b.status}, not Pending.")
            if b.equipment.usable_units == 0:
                raise BookingError("All units of this equipment are damaged; it cannot be confirmed.")
            b.status, b.decided_by_id = "Confirmed", current_user.id
            b.status_note = "Confirmed by lab staff"
            db.session.commit()
            flash(f"Booking {b.reference} confirmed.", "success")
        elif action == "reject":
            cancel_booking(b, current_user, note=(request.form.get("note") or "").strip() or "Declined by lab staff")
            flash(f"Booking {b.reference} declined.", "success")
        else:
            abort(400)
    except BookingError as exc:
        flash(str(exc), "danger")
    return redirect(back)


# ----------------------------------------------------------------- issues
@staff.route("/issues", methods=["GET", "POST"])
def issues():
    if request.method == "POST":
        issue = db.session.get(Issue, request.form.get("issue_id", type=int)) or abort(404)
        st = request.form.get("status")
        if st in ISSUE_STATUSES:
            issue.status = st
        issue.staff_note = (request.form.get("staff_note") or "").strip() or None
        db.session.commit()
        flash("Issue updated.", "success")
        return redirect(url_for("staff.issues", status=request.args.get("status")))
    st = request.args.get("status")
    q = Issue.query
    if st in ISSUE_STATUSES:
        q = q.filter_by(status=st)
    rows = q.order_by(Issue.created_at.desc()).all()
    return render_template("staff/issues.html", rows=rows, statuses=ISSUE_STATUSES, sel=st, nav="issues")


# ----------------------------------------------------------------- timetable
@staff.route("/timetable", methods=["GET", "POST"])
def timetable():
    if request.method == "POST":
        f = request.form
        lab = db.session.get(Lab, f.get("lab_id", type=int))
        start, end = parse_time(f.get("start", "")), parse_time(f.get("end", ""))
        days = [int(d) for d in f.getlist("days") if d.isdigit() and 0 <= int(d) <= 6]
        typ = f.get("activity_type")
        source = (f.get("verified_source") or "").strip()
        errs = []
        if not lab:
            errs.append("Choose a laboratory.")
        if not days:
            errs.append("Choose at least one day.")
        if not start or not end or end <= start:
            errs.append("Enter a valid start and end time.")
        if typ not in ACTIVITY_TYPES:
            errs.append("Choose an activity type.")
        if len(source) < 3:
            errs.append("Record where this slot was verified (e.g. 'ECE dept. timetable, Autumn 2026').")
        if not errs:
            for d in days:
                clash = [e for e in lab.timetable if e.day_of_week == d and overlaps(start, end, e.start_time, e.end_time)]
                if clash:
                    errs.append(f"{DAYS[d]}: overlaps existing {clash[0].activity_type} "
                                f"{clash[0].start_time:%H:%M}-{clash[0].end_time:%H:%M}.")
        if errs:
            for e in errs:
                flash(e, "danger")
        else:
            for d in days:
                db.session.add(TimetableEntry(lab_id=lab.id, day_of_week=d, start_time=start, end_time=end,
                                              activity_type=typ, title=(f.get("title") or "").strip()[:160] or None,
                                              verified_source=source[:200], created_by_id=current_user.id))
            db.session.commit()
            flash(f"Added {len(days)} slot(s) to {lab.name}.", "success")
        return redirect(url_for("staff.timetable", lab=f.get("lab_id")))
    labs = Lab.query.order_by(Lab.name).all()
    sel = request.args.get("lab", type=int)
    return render_template("staff/timetable.html", labs=labs, sel=sel, days=DAYS, types=ACTIVITY_TYPES,
                           nav="timetable")


@staff.route("/timetable/<int:entry_id>/delete", methods=["POST"])
def delete_slot(entry_id):
    e = db.session.get(TimetableEntry, entry_id) or abort(404)
    lab_id = e.lab_id
    db.session.delete(e)
    db.session.commit()
    flash("Timetable slot removed.", "success")
    return redirect(url_for("staff.timetable", lab=lab_id))


# ----------------------------------------------------------------- staff access (admins)
@staff.route("/access", methods=["GET", "POST"])
@admin_required
def access():
    new_code = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "generate":
            uses, days = _int("max_uses", 1), _int("days", 7)
            if not uses or not days or not (1 <= uses <= 50) or not (1 <= days <= 90):
                flash("Uses must be 1–50 and validity 1–90 days.", "danger")
            else:
                new_code, code = generate_access_code(request.form.get("label"), uses, days, current_user)
                flash("Access code created. Copy it now — it will not be shown again.", "success")
        elif action == "revoke":
            code = db.session.get(StaffAccessCode, request.form.get("code_id", type=int)) or abort(404)
            code.revoked = True
            db.session.commit()
            flash(f"Code ending …{code.hint} revoked.", "success")
            return redirect(url_for("staff.access"))
        elif action in ("disable", "enable", "make_admin", "remove_admin"):
            user = db.session.get(User, request.form.get("user_id", type=int)) or abort(404)
            if user.role != "staff" or user.id == current_user.id:
                flash("You cannot change your own access here.", "danger")
            else:
                if action in ("disable", "enable"):
                    user.is_enabled = action == "enable"
                    user.token_version += 1    # ends their sessions
                else:
                    user.is_admin = action == "make_admin"
                db.session.commit()
                flash(f"Updated {user.full_name}.", "success")
            return redirect(url_for("staff.access"))
    codes = StaffAccessCode.query.order_by(StaffAccessCode.created_at.desc()).all()
    team = User.query.filter_by(role="staff").order_by(User.full_name).all()
    return render_template("staff/access.html", codes=codes, team=team, new_code=new_code, nav="access")
