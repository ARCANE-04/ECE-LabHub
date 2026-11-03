"""Domain logic: laboratory accessibility, equipment availability, booking, inventory stats.

Laboratory accessibility (is the *room* open, in class, reserved?) comes only from the
verified timetable. Equipment availability (how many *units* are free in a window) comes from
equipment quantity, condition, access level and overlapping bookings. The two are kept
separate; booking checks both.
"""
import secrets
import string
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import and_, func, or_, text, update

from .extensions import db
from .models import (ACTIVE_BOOKING_STATUSES, CONDITIONS, RECORD_STATUSES, Booking, Equipment, Lab,
                     TimetableEntry)


class BookingError(Exception):
    pass


# ----------------------------------------------------------------------------- time
def now_local():
    tz = ZoneInfo(current_app.config["TIMEZONE"])
    return datetime.now(tz).replace(tzinfo=None, microsecond=0)


def parse_time(value):
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        return None


def parse_date(value):
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


# ----------------------------------------------------------------- lab accessibility
def lab_entries_on(lab, weekday):
    return [e for e in lab.timetable if e.day_of_week == weekday]


def lab_status(lab, at=None):
    """Return a dict describing laboratory accessibility at a moment (from the timetable only)."""
    at = at or now_local()
    if not lab.timetable:
        return {"state": "not_provided", "label": "Schedule not provided",
                "detail": "No verified timetable has been provided for this laboratory yet."}
    current = [e for e in lab_entries_on(lab, at.weekday()) if e.start_time <= at.time() < e.end_time]
    priority = {"Class": 0, "Reserved": 1, "Open Access": 2}
    if current:
        e = sorted(current, key=lambda x: priority[x.activity_type])[0]
        state = {"Class": "class", "Reserved": "reserved", "Open Access": "open"}[e.activity_type]
        label = {"class": "Class in progress", "reserved": "Reserved", "open": "Open access"}[state]
        return {"state": state, "label": label, "entry": e,
                "detail": f"{e.title or e.activity_type} until {e.end_time:%H:%M}"}
    upcoming = sorted([e for e in lab_entries_on(lab, at.weekday()) if e.start_time > at.time()],
                      key=lambda x: x.start_time)
    nxt = upcoming[0] if upcoming else None
    return {"state": "unscheduled", "label": "No scheduled session", "entry": nxt,
            "detail": (f"Next: {nxt.activity_type} at {nxt.start_time:%H:%M}" if nxt
                       else "Nothing further scheduled today")}


def lab_window_conflicts(lab, on_date, start, end):
    """Class / Reserved timetable slots that overlap a requested window."""
    return [e for e in lab_entries_on(lab, on_date.weekday())
            if e.activity_type in ("Class", "Reserved") and overlaps(start, end, e.start_time, e.end_time)]


# ------------------------------------------------------------- equipment availability
def booking_blockers(eq):
    """Reasons an item can never be booked by a student right now (independent of time)."""
    reasons = []
    if eq.quantity > 0 and eq.usable_units == 0:
        reasons.append("All units are marked damaged by lab staff")
    if eq.access_level == "Research only":
        reasons.append("Restricted to research use (per lab inventory)")
    if not eq.bookable:
        reasons.append("Withdrawn from booking by lab staff")
    if not eq.lab.booking_enabled:
        reasons.append("Booking is disabled for this laboratory")
    if eq.quantity <= 0:
        reasons.append("No units in inventory")
    return reasons


def booked_quantity(equipment_id, on_date, start, end, exclude_booking_id=None):
    q = db.session.query(func.coalesce(func.sum(Booking.quantity), 0)).filter(
        Booking.equipment_id == equipment_id,
        Booking.booking_date == on_date,
        Booking.status.in_(ACTIVE_BOOKING_STATUSES),
        Booking.start_time < end,
        Booking.end_time > start,
    )
    if exclude_booking_id:
        q = q.filter(Booking.id != exclude_booking_id)
    return int(q.scalar() or 0)


def peak_booked_quantity(equipment_id, on_date, start, end):
    """Max simultaneous units booked at any instant inside [start, end).

    Summing every overlapping booking would over-count two bookings that overlap the window
    but not each other; the true constraint is the peak concurrent load.
    """
    rows = Booking.query.filter(
        Booking.equipment_id == equipment_id, Booking.booking_date == on_date,
        Booking.status.in_(ACTIVE_BOOKING_STATUSES), Booking.start_time < end, Booking.end_time > start,
    ).all()
    events = []
    for b in rows:
        events.append((max(b.start_time, start), 1, b.quantity))
        events.append((min(b.end_time, end), 0, -b.quantity))  # ends sort before starts at same instant
    load = peak = 0
    for _t, _kind, delta in sorted(events):
        load += delta
        peak = max(peak, load)
    return peak


def availability(eq, on_date, start, end):
    blockers = booking_blockers(eq)
    booked = peak_booked_quantity(eq.id, on_date, start, end)
    free = max(eq.usable_units - booked, 0)
    lab_conflicts = lab_window_conflicts(eq.lab, on_date, start, end)
    return {
        "total": eq.quantity, "usable": eq.usable_units, "damaged": eq.units_damaged,
        "booked": booked, "available": 0 if blockers else free,
        "blockers": blockers,
        "lab_conflicts": [f"{c.activity_type}: {c.title or ''} {c.start_time:%H:%M}-{c.end_time:%H:%M}".strip()
                          for c in lab_conflicts],
    }


def current_availability(eq):
    now = now_local()
    start = now.time()
    end = (now + timedelta(minutes=1)).time() if now.time() < time(23, 59) else time(23, 59, 59)
    return availability(eq, now.date(), start, end)


# ------------------------------------------------------------------------ bookings
def new_reference():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        ref = "LB-" + now_local().strftime("%y%m") + "-" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not Booking.query.filter_by(reference=ref).first():
            return ref


DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def booking_breaks():
    """Configured no-booking breaks (e.g. lunch) as (start, end) time pairs."""
    out = []
    for b in current_app.config.get("BOOKING_BREAKS", []):
        s, _, e = b.partition("-")
        s, e = parse_time(s), parse_time(e)
        if s and e and e > s:
            out.append((s, e))
    return out


def booking_hours_text():
    cfg = current_app.config
    days = sorted(cfg.get("BOOKING_WEEKDAYS", range(7)))
    if days == [0, 1, 2, 3, 4]:
        day_txt = "Monday–Friday"
    elif len(days) == 7:
        day_txt = "every day"
    else:
        day_txt = ", ".join(DAY_NAMES[d][:3] for d in days)
    txt = f"{day_txt}, {cfg['BOOKING_DAY_START']}–{cfg['BOOKING_DAY_END']}"
    brk = booking_breaks()
    if brk:
        txt += " (no bookings " + ", ".join(f"{s:%H:%M}–{e:%H:%M}" for s, e in brk) + ")"
    return txt


def booking_rule_error(on_date, start, end):
    """Booking-hours rules (allowed weekdays, day start/end, lunch break). Returns a message or None."""
    cfg = current_app.config
    allowed = cfg.get("BOOKING_WEEKDAYS", list(range(7)))
    if on_date.weekday() not in allowed:
        return (f"Equipment cannot be booked on a {DAY_NAMES[on_date.weekday()]}. "
                f"Bookings are allowed {booking_hours_text()}.")
    open_t, close_t = parse_time(cfg["BOOKING_DAY_START"]), parse_time(cfg["BOOKING_DAY_END"])
    if start < open_t or end > close_t:
        return f"Bookings must fall between {cfg['BOOKING_DAY_START']} and {cfg['BOOKING_DAY_END']}."
    for b_s, b_e in booking_breaks():
        if overlaps(start, end, b_s, b_e):
            return f"Equipment cannot be booked during the lunch break ({b_s:%H:%M}–{b_e:%H:%M})."
    return None


def validate_window(on_date, start, end):
    cfg = current_app.config
    if not on_date or not start or not end:
        raise BookingError("Please choose a valid date, start time and end time.")
    if end <= start:
        raise BookingError("End time must be after start time.")
    now = now_local()
    if datetime.combine(on_date, start) <= now:
        raise BookingError("The booking must start in the future.")
    if on_date > now.date() + timedelta(days=cfg["BOOKING_MAX_DAYS_AHEAD"]):
        raise BookingError(f"Bookings can be made at most {cfg['BOOKING_MAX_DAYS_AHEAD']} days in advance.")
    minutes = (datetime.combine(on_date, end) - datetime.combine(on_date, start)).seconds // 60
    if minutes < cfg["BOOKING_MIN_MINUTES"]:
        raise BookingError(f"Minimum booking length is {cfg['BOOKING_MIN_MINUTES']} minutes.")
    if minutes > cfg["BOOKING_MAX_HOURS"] * 60:
        raise BookingError(f"Maximum booking length is {cfg['BOOKING_MAX_HOURS']} hours.")
    rule = booking_rule_error(on_date, start, end)
    if rule:
        raise BookingError(rule)


def create_booking(user, equipment_id, on_date, start, end, quantity, purpose):
    """Validate and save a booking atomically. Returns the saved Booking.

    Concurrency: the first statement of the transaction is a write on the equipment row,
    which takes SQLite's RESERVED lock. Any concurrent booking transaction blocks at that
    point (busy_timeout) until this one commits, then re-reads availability - so two requests
    can never both see the same free unit.
    """
    if user.role != "student" or not user.is_verified:
        raise BookingError("Only verified student accounts can book equipment.")
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise BookingError("Quantity must be a whole number.")
    if quantity < 1:
        raise BookingError("Quantity must be at least 1.")
    validate_window(on_date, start, end)

    db.session.commit()  # close any implicit read transaction from earlier in the request
    try:
        # Acquire the write lock first (no-op update) - serialises concurrent bookings.
        db.session.execute(update(Equipment).where(Equipment.id == equipment_id)
                           .values(quantity=Equipment.quantity, updated_at=Equipment.updated_at))
        eq = db.session.get(Equipment, equipment_id, populate_existing=True)
        if eq is None:
            raise BookingError("Equipment not found.")
        blockers = booking_blockers(eq)
        if blockers:
            raise BookingError("This equipment cannot be booked: " + "; ".join(blockers) + ".")
        conflicts = lab_window_conflicts(eq.lab, on_date, start, end)
        if conflicts:
            c = conflicts[0]
            raise BookingError(f"{eq.lab.name} has a {c.activity_type.lower()} slot "
                               f"{c.start_time:%H:%M}-{c.end_time:%H:%M} on that day; choose another time.")
        dup = Booking.query.filter(
            Booking.user_id == user.id, Booking.equipment_id == eq.id, Booking.booking_date == on_date,
            Booking.status.in_(ACTIVE_BOOKING_STATUSES), Booking.start_time < end, Booking.end_time > start,
        ).first()
        if dup:
            raise BookingError(f"You already have booking {dup.reference} for this equipment in an "
                               f"overlapping time window.")
        max_qty = current_app.config["BOOKING_MAX_UNITS"]
        if quantity > max_qty:
            raise BookingError(f"You can book at most {max_qty} units per booking.")
        booked = peak_booked_quantity(eq.id, on_date, start, end)
        free = eq.usable_units - booked          # damaged units are never bookable
        if free <= 0:
            raise BookingError("No working units are available in that time window.")
        if quantity > free:
            raise BookingError(f"Only {free} unit(s) are available in that time window.")

        if booked + quantity <= eq.verified_usable_units:
            # enough units inspected and working to cover this request -> confirm immediately
            status, note = "Confirmed", None
            if booked + quantity > eq.units_functional:
                note = "Includes a partially functional unit - check with lab staff before use."
        else:  # relies on units nobody has inspected yet -> hold them, staff confirm
            status = "Pending"
            note = "Some units have not been inspected yet; lab staff will confirm this request."
        booking = Booking(reference=new_reference(), user_id=user.id, equipment_id=eq.id,
                          booking_date=on_date, start_time=start, end_time=end, quantity=quantity,
                          purpose=(purpose or "").strip()[:300] or None, status=status, status_note=note)
        db.session.add(booking)
        db.session.commit()
        return booking
    except BookingError:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        raise


def cancel_booking(booking, by_user, note=None):
    if booking.status not in ACTIVE_BOOKING_STATUSES:
        raise BookingError(f"Booking {booking.reference} is already {booking.status.lower()}.")
    if booking.ends_at <= now_local():
        raise BookingError("This booking has already ended and cannot be cancelled.")
    booking.status = "Cancelled"
    booking.status_note = note or ("Cancelled by student" if by_user.role == "student" else "Cancelled by lab staff")
    if by_user.role == "staff":
        booking.decided_by_id = by_user.id
    db.session.commit()


def refresh_booking_statuses():
    """Confirmed bookings whose end time passed -> Completed; Pending ones that were never
    confirmed before they ended -> Cancelled."""
    now = now_local()
    ended = or_(Booking.booking_date < now.date(),
                and_(Booking.booking_date == now.date(), Booking.end_time <= now.time()))
    n1 = Booking.query.filter(Booking.status == "Confirmed", ended).update(
        {"status": "Completed"}, synchronize_session=False)
    n2 = Booking.query.filter(Booking.status == "Pending", ended).update(
        {"status": "Cancelled", "status_note": "Lapsed - not confirmed by lab staff before the slot ended"},
        synchronize_session=False)
    if n1 or n2:
        db.session.commit()


def rebalance_bookings(eq, reason):
    """After usable capacity drops (units marked damaged), keep upcoming bookings in the order
    they were made and cancel only those that no longer fit. Returns the number cancelled."""
    now = now_local()
    future = or_(Booking.booking_date > now.date(),
                 and_(Booking.booking_date == now.date(), Booking.end_time > now.time()))
    rows = (Booking.query.filter(Booking.equipment_id == eq.id, Booking.status.in_(ACTIVE_BOOKING_STATUSES), future)
            .order_by(Booking.created_at, Booking.id).all())
    capacity, kept, cancelled = eq.usable_units, [], 0
    for b in rows:
        same_day = [k for k in kept if k.booking_date == b.booking_date
                    and overlaps(k.start_time, k.end_time, b.start_time, b.end_time)]
        events = []
        for k in same_day:
            events += [(max(k.start_time, b.start_time), 1, k.quantity), (min(k.end_time, b.end_time), 0, -k.quantity)]
        load = peak = 0
        for _t, _k, d in sorted(events):
            load += d
            peak = max(peak, load)
        if peak + b.quantity <= capacity:
            kept.append(b)
        else:
            b.status, b.status_note = "Cancelled", reason
            cancelled += 1
    return cancelled


def cancel_future_bookings_for(eq, reason):
    now = now_local()
    future = or_(Booking.booking_date > now.date(),
                 and_(Booking.booking_date == now.date(), Booking.end_time > now.time()))
    rows = Booking.query.filter(Booking.equipment_id == eq.id,
                                Booking.status.in_(ACTIVE_BOOKING_STATUSES), future).all()
    for b in rows:
        b.status = "Cancelled"
        b.status_note = reason
    return len(rows)


# ------------------------------------------------------------------ inventory stats
def inventory_stats(lab_id=None):
    """All figures computed from the database.

    records   - number of equipment rows
    quantity  - physical units (sum of quantities)
    units     - physical units per condition (Functional / Partially Functional / Damaged / Unknown)
    by_status - records per derived record status, with their unit totals
    """
    base = db.session.query(Equipment)
    if lab_id:
        base = base.filter(Equipment.lab_id == lab_id)
    agg = base.with_entities(
        func.count(Equipment.id),
        func.coalesce(func.sum(Equipment.quantity), 0),
        func.coalesce(func.sum(Equipment.units_functional), 0),
        func.coalesce(func.sum(Equipment.units_partial), 0),
        func.coalesce(func.sum(Equipment.units_damaged), 0),
    ).one()
    records, qty, f, p, d = (int(x) for x in agg)
    units = {"Functional": f, "Partially Functional": p, "Damaged": d, "Unknown": qty - f - p - d}
    by_status = {c: {"records": 0, "quantity": 0} for c in RECORD_STATUSES}
    for cond, n, q in (base.with_entities(Equipment.condition, func.count(Equipment.id),
                                          func.coalesce(func.sum(Equipment.quantity), 0))
                       .group_by(Equipment.condition).all()):
        by_status[cond] = {"records": int(n), "quantity": int(q)}
    with_damage = base.filter(Equipment.units_damaged > 0).count()
    inspected = qty - units["Unknown"]
    return {"records": records, "quantity": qty, "units": units, "by_status": by_status,
            "records_with_damage": with_damage, "inspected_pct": round(inspected / qty * 100) if qty else 0}


def lab_breakdown():
    return [{"lab": lab, **inventory_stats(lab.id)} for lab in Lab.query.order_by(Lab.name).all()]


def unit_summary(eq):
    parts = []
    for label, n in (("working", eq.units_functional), ("partial", eq.units_partial),
                     ("damaged", eq.units_damaged), ("unverified", eq.units_unknown)):
        if n:
            parts.append(f"{n} {label}")
    return " · ".join(parts) or "no units"
