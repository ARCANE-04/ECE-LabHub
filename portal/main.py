"""Public + student portal."""
from datetime import datetime, timedelta

from flask import (Blueprint, abort, flash, jsonify, redirect, render_template, request,
                   url_for)
from flask_login import current_user
from sqlalchemy import func, or_

from .auth import student_required
from .extensions import db
from flask_login import login_required
from .models import (ACTIVITY_TYPES, DAYS, ISSUE_SEVERITIES, RECORD_STATUSES, Booking, Equipment,
                     Issue, Keyword, Lab, TimetableEntry, equipment_keywords)
from .services import (BookingError, availability, booking_blockers, booking_breaks, booking_hours_text,
                       booking_rule_error, cancel_booking,
                       create_booking, current_availability, inventory_stats, lab_status,
                       now_local, parse_date, parse_time, refresh_booking_statuses)

main = Blueprint("main", __name__)


@main.before_request
def _require_sign_in():
    """The portal opens on the sign-in / sign-up gateway; everything else needs an account."""
    if request.endpoint == "main.home" or current_user.is_authenticated:
        return None
    from .auth import unauthorized
    return unauthorized()


def equipment_query(q=None, lab_code=None, category=None, condition=None, bookable_only=False):
    query = Equipment.query.join(Lab)
    if q:
        like = f"%{q.strip()}%"
        kw_ids = (db.session.query(equipment_keywords.c.equipment_id)
                  .join(Keyword, Keyword.id == equipment_keywords.c.keyword_id)
                  .filter(Keyword.word.ilike(like)))
        query = query.filter(or_(Equipment.name.ilike(like), Equipment.manufacturer.ilike(like),
                                 Equipment.model.ilike(like), Equipment.specification.ilike(like),
                                 Equipment.category.ilike(like), Lab.name.ilike(like),
                                 Equipment.id.in_(kw_ids)))
    if lab_code:
        query = query.filter(Lab.code == lab_code)
    if category:
        query = query.filter(Equipment.category == category)
    if condition in RECORD_STATUSES:
        query = query.filter(Equipment.condition == condition)
    if bookable_only:
        query = query.filter(Equipment.quantity > Equipment.units_damaged, Equipment.access_level != "Research only",
                             Equipment.bookable.is_(True), Lab.booking_enabled.is_(True))
    return query


@main.route("/")
def home():
    if not current_user.is_authenticated:
        return render_template("auth/gateway.html")
    if current_user.role == "staff":
        return redirect(url_for("staff.dashboard"))
    refresh_booking_statuses()
    labs = Lab.query.order_by(Lab.name).all()
    stats = inventory_stats()
    categories = (db.session.query(Equipment.category, func.count(Equipment.id), func.sum(Equipment.quantity))
                  .group_by(Equipment.category).order_by(func.sum(Equipment.quantity).desc()).limit(8).all())
    now = now_local()
    upcoming = (Booking.query.filter(Booking.user_id == current_user.id, Booking.status.in_(("Pending", "Confirmed")))
                .order_by(Booking.booking_date, Booking.start_time).limit(4).all())
    return render_template("student/home.html", labs=labs, stats=stats, categories=categories, upcoming=upcoming,
                           statuses={lab.id: lab_status(lab) for lab in labs}, now=now, nav="home")


@main.route("/labs")
def labs():
    labs = Lab.query.order_by(Lab.name).all()
    counts = {lab.id: inventory_stats(lab.id) for lab in labs}
    statuses = {lab.id: lab_status(lab) for lab in labs}
    return render_template("student/labs.html", labs=labs, counts=counts, statuses=statuses, nav="labs")


@main.route("/labs/<code>")
def lab_detail(code):
    lab = Lab.query.filter_by(code=code).first_or_404()
    stats = inventory_stats(lab.id)
    cats = sorted({e.category for e in lab.equipment})
    return render_template("student/lab_detail.html", lab=lab, stats=stats, categories=cats,
                           status=lab_status(lab), days=DAYS, nav="labs")


@main.route("/equipment")
def equipment_list():
    q = request.args.get("q", "").strip()
    lab_code = request.args.get("lab") or None
    category = request.args.get("category") or None
    condition = request.args.get("condition") or None
    bookable_only = request.args.get("bookable") == "1"
    items = (equipment_query(q, lab_code, category, condition, bookable_only)
             .order_by(Lab.name, Equipment.category, Equipment.name).all())
    categories = [c for (c,) in db.session.query(Equipment.category).distinct().order_by(Equipment.category)]
    return render_template("student/equipment_list.html", items=items, q=q, labs=Lab.query.order_by(Lab.name).all(),
                           categories=categories, conditions=RECORD_STATUSES, sel_lab=lab_code, sel_cat=category,
                           sel_cond=condition, bookable_only=bookable_only, nav="equipment")


@main.route("/equipment/<int:eq_id>")
def equipment_detail(eq_id):
    eq = db.session.get(Equipment, eq_id) or abort(404)
    refresh_booking_statuses()
    now = now_local()
    sel_date = parse_date(request.args.get("date", "")) or now.date()
    day_bookings = (Booking.query.filter(Booking.equipment_id == eq.id, Booking.booking_date == sel_date,
                                         Booking.status.in_(("Pending", "Confirmed")))
                    .order_by(Booking.start_time).all())
    related = (Equipment.query.filter(Equipment.category == eq.category, Equipment.id != eq.id)
               .limit(4).all())
    return render_template("student/equipment_detail.html", eq=eq, now_avail=current_availability(eq),
                           blockers=booking_blockers(eq), day_bookings=day_bookings, sel_date=sel_date,
                           min_date=now.date(), max_date=now.date() + timedelta(days=30),
                           lab_st=lab_status(eq.lab), related=related, severities=ISSUE_SEVERITIES,
                           lab=eq.lab, days=DAYS, hours=list(range(8, 19)), now=now,
                           booking_hours=booking_hours_text(), booking_breaks=booking_breaks(),
                           booking_weekdays=config_weekdays(), nav="equipment")


def config_weekdays():
    from flask import current_app
    return list(current_app.config.get("BOOKING_WEEKDAYS", range(7)))


@main.route("/equipment/<int:eq_id>/book", methods=["POST"])
@student_required
def book(eq_id):
    eq = db.session.get(Equipment, eq_id) or abort(404)
    on_date = parse_date(request.form.get("date", ""))
    start, end = parse_time(request.form.get("start", "")), parse_time(request.form.get("end", ""))
    try:
        booking = create_booking(current_user, eq.id, on_date, start, end,
                                 request.form.get("quantity", "1"), request.form.get("purpose"))
    except BookingError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("main.equipment_detail", eq_id=eq_id,
                                date=request.form.get("date", "")) + "#book")
    return redirect(url_for("main.booking_detail", ref=booking.reference, new=1))


@main.route("/bookings")
@student_required
def my_bookings():
    refresh_booking_statuses()
    now = now_local()
    rows = (Booking.query.filter_by(user_id=current_user.id)
            .order_by(Booking.booking_date.desc(), Booking.start_time.desc()).all())
    current = [b for b in rows if b.is_active]
    current.sort(key=lambda b: b.starts_at)
    past = [b for b in rows if not b.is_active]
    return render_template("student/my_bookings.html", current=current, past=past, now=now, nav="bookings")


@main.route("/bookings/<ref>")
@student_required
def booking_detail(ref):
    refresh_booking_statuses()
    b = Booking.query.filter_by(reference=ref, user_id=current_user.id).first_or_404()
    return render_template("student/booking_detail.html", b=b, new=request.args.get("new") == "1",
                           can_cancel=b.is_active and b.ends_at > now_local(), nav="bookings")


@main.route("/bookings/<ref>/cancel", methods=["POST"])
@student_required
def booking_cancel(ref):
    b = Booking.query.filter_by(reference=ref, user_id=current_user.id).first_or_404()
    try:
        cancel_booking(b, current_user)
        flash(f"Booking {b.reference} cancelled.", "success")
    except BookingError as exc:
        flash(str(exc), "danger")
    return redirect(request.form.get("back") == "list" and url_for("main.my_bookings")
                    or url_for("main.booking_detail", ref=ref))


@main.route("/equipment/<int:eq_id>/report-issue", methods=["POST"])
@student_required
def report_issue(eq_id):
    eq = db.session.get(Equipment, eq_id) or abort(404)
    title = (request.form.get("title") or "").strip()
    desc = (request.form.get("description") or "").strip()
    sev = request.form.get("severity") if request.form.get("severity") in ISSUE_SEVERITIES else "Medium"
    if len(title) < 4 or len(desc) < 10:
        flash("Please give the issue a short title and describe what is wrong (10+ characters).", "danger")
        return redirect(url_for("main.equipment_detail", eq_id=eq_id) + "#report")
    db.session.add(Issue(equipment_id=eq.id, reporter_id=current_user.id, title=title[:160],
                         description=desc[:4000], severity=sev))
    db.session.commit()
    flash("Thank you - the issue has been reported to lab staff.", "success")
    return redirect(url_for("main.equipment_detail", eq_id=eq_id))


@main.route("/timetable")
def timetable():
    labs = Lab.query.order_by(Lab.name).all()
    sel = request.args.get("lab")
    shown = [l for l in labs if l.code == sel] if sel else labs
    check = None
    c_date, c_time = request.args.get("date"), request.args.get("time")
    if c_date or c_time:
        d, t = parse_date(c_date or ""), parse_time(c_time or "")
        if d and t:
            moment = datetime.combine(d, t)
            check = {"moment": moment, "results": [(l, lab_status(l, moment)) for l in shown]}
        else:
            flash("Enter a valid date and time to check laboratory availability.", "danger")
    hours = list(range(8, 21))
    return render_template("student/timetable.html", labs=labs, shown=shown, sel=sel, days=DAYS,
                           hours=hours, check=check, now=now_local(), types=ACTIVITY_TYPES,
                           statuses={l.id: lab_status(l) for l in shown}, nav="timetable")


@main.route("/about")
def about():
    labs = Lab.query.order_by(Lab.name).all()
    return render_template("student/about.html", labs=labs, stats=inventory_stats(), nav="about")


@main.route("/profile")
@student_required
def profile():
    counts = dict(db.session.query(Booking.status, func.count(Booking.id))
                  .filter(Booking.user_id == current_user.id).group_by(Booking.status).all())
    issues = Issue.query.filter_by(reporter_id=current_user.id).order_by(Issue.created_at.desc()).all()
    return render_template("student/profile.html", counts=counts, issues=issues, nav="profile")


# ------------------------------------------------------------------ JSON API
@main.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    rows = equipment_query(q).order_by(Equipment.name).limit(8).all()
    return jsonify([{"id": e.id, "name": e.title, "lab": e.lab.name, "category": e.category,
                     "url": url_for("main.equipment_detail", eq_id=e.id)} for e in rows])


@main.route("/api/equipment/<int:eq_id>/availability")
def api_availability(eq_id):
    eq = db.session.get(Equipment, eq_id) or abort(404)
    d = parse_date(request.args.get("date", ""))
    s, e = parse_time(request.args.get("start", "")), parse_time(request.args.get("end", ""))
    if not (d and s and e) or e <= s:
        return jsonify({"error": "Choose a valid date and a start time before the end time."}), 400
    rule = booking_rule_error(d, s, e)
    if rule:
        return jsonify({"error": rule}), 400
    return jsonify(availability(eq, d, s, e))


@main.route("/api/labs/<code>/status")
def api_lab_status(code):
    lab = Lab.query.filter_by(code=code).first_or_404()
    st = lab_status(lab)
    return jsonify({"lab": lab.name, "room": lab.room_number, "state": st["state"], "label": st["label"],
                    "detail": st["detail"]})
