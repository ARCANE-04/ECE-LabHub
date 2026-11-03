from datetime import datetime, date, time, timezone

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db

# Condition of an individual physical unit
CONDITIONS = ["Functional", "Partially Functional", "Damaged", "Unknown"]
# Summary status of an equipment record (derived from its unit counts)
RECORD_STATUSES = ["Functional", "Partially Functional", "Mixed", "Damaged", "Unknown"]
UNIT_FIELDS = {"Functional": "units_functional", "Partially Functional": "units_partial",
               "Damaged": "units_damaged"}
ACCESS_LEVELS = ["UG/PG Labs & Research", "Research only", "Not specified"]
ACTIVITY_TYPES = ["Class", "Reserved", "Open Access"]
BOOKING_STATUSES = ["Pending", "Confirmed", "Cancelled", "Completed"]
ACTIVE_BOOKING_STATUSES = ("Pending", "Confirmed")
ISSUE_STATUSES = ["Open", "In Progress", "Resolved"]
ISSUE_SEVERITIES = ["Low", "Medium", "High"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


equipment_keywords = db.Table(
    "equipment_keywords",
    db.Column("equipment_id", db.Integer, db.ForeignKey("equipment.id", ondelete="CASCADE"), primary_key=True),
    db.Column("keyword_id", db.Integer, db.ForeignKey("keyword.id", ondelete="CASCADE"), primary_key=True),
)


class Lab(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(160), nullable=False)
    room_number = db.Column(db.String(64))          # NULL until staff enter the verified room
    building = db.Column(db.String(160))
    description = db.Column(db.Text)
    source_file = db.Column(db.String(200))
    booking_enabled = db.Column(db.Boolean, nullable=False, default=True)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    equipment = db.relationship("Equipment", back_populates="lab", cascade="all, delete-orphan",
                                order_by="Equipment.id")
    timetable = db.relationship("TimetableEntry", back_populates="lab", cascade="all, delete-orphan",
                                order_by="(TimetableEntry.day_of_week, TimetableEntry.start_time)")

    @property
    def room_display(self):
        return self.room_number or "Room not yet provided"


class Keyword(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    word = db.Column(db.String(80), unique=True, nullable=False)


class Equipment(db.Model):
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_equipment_quantity"),
        CheckConstraint("condition IN ('Functional','Partially Functional','Mixed','Damaged','Unknown')",
                        name="ck_equipment_condition"),
        CheckConstraint("units_functional >= 0 AND units_partial >= 0 AND units_damaged >= 0 AND "
                        "units_functional + units_partial + units_damaged <= quantity", name="ck_equipment_units"),
        Index("ix_equipment_lab_condition", "lab_id", "condition"),
    )
    id = db.Column(db.Integer, primary_key=True)
    lab_id = db.Column(db.Integer, db.ForeignKey("lab.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(80), nullable=False, index=True)
    manufacturer = db.Column(db.String(120))
    model = db.Column(db.String(120))
    specification = db.Column(db.String(255))
    quantity = db.Column(db.Integer, nullable=False, default=1)
    # Unit-level condition. Units not counted in any of these are "Unknown" (not yet inspected).
    units_functional = db.Column(db.Integer, nullable=False, default=0)
    units_partial = db.Column(db.Integer, nullable=False, default=0)
    units_damaged = db.Column(db.Integer, nullable=False, default=0)
    condition = db.Column(db.String(32), nullable=False, default="Unknown")  # derived summary, see refresh_condition()
    access_level = db.Column(db.String(40), nullable=False, default="Not specified")
    bookable = db.Column(db.Boolean, nullable=False, default=True)   # staff can withdraw an item
    notes = db.Column(db.Text)
    source_text = db.Column(db.Text)
    source_ref = db.Column(db.String(120))
    image_path = db.Column(db.String(500))       # static-relative path or absolute https URL
    image_kind = db.Column(db.String(20), default="illustration")   # illustration | photo
    image_credit = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    lab = db.relationship("Lab", back_populates="equipment")
    keywords = db.relationship("Keyword", secondary=equipment_keywords, lazy="selectin",
                               order_by="Keyword.word")
    bookings = db.relationship("Booking", back_populates="equipment", cascade="all, delete-orphan")
    issues = db.relationship("Issue", back_populates="equipment", cascade="all, delete-orphan")

    @property
    def units_unknown(self):
        return max((self.quantity or 0) - (self.units_functional or 0) - (self.units_partial or 0)
                   - (self.units_damaged or 0), 0)

    @property
    def usable_units(self):
        """Units that may be booked: everything except damaged units."""
        return max((self.quantity or 0) - (self.units_damaged or 0), 0)

    @property
    def verified_usable_units(self):
        return (self.units_functional or 0) + (self.units_partial or 0)

    @property
    def unit_breakdown(self):
        return {"Functional": self.units_functional or 0, "Partially Functional": self.units_partial or 0,
                "Damaged": self.units_damaged or 0, "Unknown": self.units_unknown}

    def set_units(self, functional, partial, damaged):
        if min(functional, partial, damaged) < 0 or functional + partial + damaged > self.quantity:
            raise ValueError("Unit counts must be ≥ 0 and add up to at most the total quantity.")
        self.units_functional, self.units_partial, self.units_damaged = functional, partial, damaged
        self.refresh_condition()

    def refresh_condition(self):
        q, b = self.quantity or 0, self.unit_breakdown
        if q == 0 or b["Unknown"] == q:
            self.condition = "Unknown"
        elif b["Damaged"] == q:
            self.condition = "Damaged"
        elif b["Functional"] == q:
            self.condition = "Functional"
        elif b["Partially Functional"] == q:
            self.condition = "Partially Functional"
        else:
            self.condition = "Mixed"
        return self.condition

    @property
    def title(self):
        parts = [self.name]
        if self.manufacturer and self.manufacturer.lower() not in self.name.lower():
            parts.append(f"— {self.manufacturer}")
        if self.model and self.model.lower() not in self.name.lower():
            parts.append(self.model)
        return " ".join(parts)


class User(UserMixin, db.Model):
    __table_args__ = (CheckConstraint("role IN ('student','staff')", name="ck_user_role"),)
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(160), nullable=False)
    role = db.Column(db.String(16), nullable=False)
    enrollment_no = db.Column(db.String(32))
    department = db.Column(db.String(160))
    designation = db.Column(db.String(120))      # staff only
    is_admin = db.Column(db.Boolean, nullable=False, default=False)   # staff who manage access codes
    joined_with_code_id = db.Column(db.Integer, db.ForeignKey("staff_access_code.id", use_alter=True, name="fk_user_access_code"))
    email_verified_at = db.Column(db.DateTime)
    is_enabled = db.Column(db.Boolean, nullable=False, default=True)
    token_version = db.Column(db.Integer, nullable=False, default=0)  # bump -> old tokens die
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    last_login_at = db.Column(db.DateTime)

    bookings = db.relationship("Booking", back_populates="user", foreign_keys="Booking.user_id")
    joined_with_code = db.relationship("StaffAccessCode", foreign_keys=[joined_with_code_id])

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)
        self.token_version = (self.token_version or 0) + 1

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    @property
    def is_active(self):  # Flask-Login
        return bool(self.is_enabled)

    @property
    def is_verified(self):
        return self.email_verified_at is not None

    def get_id(self):
        # role + token_version in the session id: password reset / disable logs out other sessions
        return f"{self.id}:{self.token_version}"


class Booking(db.Model):
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_booking_quantity"),
        CheckConstraint("end_time > start_time", name="ck_booking_window"),
        CheckConstraint("status IN ('Pending','Confirmed','Cancelled','Completed')", name="ck_booking_status"),
        Index("ix_booking_equipment_date", "equipment_id", "booking_date", "status"),
    )
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(24), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    equipment_id = db.Column(db.Integer, db.ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False)
    booking_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    purpose = db.Column(db.String(300))
    status = db.Column(db.String(16), nullable=False, default="Pending")
    status_note = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)
    decided_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))

    user = db.relationship("User", back_populates="bookings", foreign_keys=[user_id])
    equipment = db.relationship("Equipment", back_populates="bookings")

    @property
    def starts_at(self):
        return datetime.combine(self.booking_date, self.start_time)

    @property
    def ends_at(self):
        return datetime.combine(self.booking_date, self.end_time)

    @property
    def is_active(self):
        return self.status in ACTIVE_BOOKING_STATUSES


class TimetableEntry(db.Model):
    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="ck_tt_day"),
        CheckConstraint("end_time > start_time", name="ck_tt_window"),
        CheckConstraint("activity_type IN ('Class','Reserved','Open Access')", name="ck_tt_type"),
    )
    id = db.Column(db.Integer, primary_key=True)
    lab_id = db.Column(db.Integer, db.ForeignKey("lab.id", ondelete="CASCADE"), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    activity_type = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(160))
    verified_source = db.Column(db.String(200), nullable=False)  # who/what the slot was verified against
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    lab = db.relationship("Lab", back_populates="timetable")

    @property
    def day_name(self):
        return DAYS[self.day_of_week]


class Issue(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    equipment_id = db.Column(db.Integer, db.ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False)
    reporter_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=False)
    severity = db.Column(db.String(10), nullable=False, default="Medium")
    status = db.Column(db.String(16), nullable=False, default="Open")
    staff_note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    equipment = db.relationship("Equipment", back_populates="issues")
    reporter = db.relationship("User")


class StaffAccessCode(db.Model):
    """Single-purpose code an administrator issues so a staff member can create an account.

    Only a SHA-256 digest of the code is stored; the plain code is shown once when generated.
    """
    __tablename__ = "staff_access_code"
    id = db.Column(db.Integer, primary_key=True)
    code_hash = db.Column(db.String(64), unique=True, nullable=False)
    hint = db.Column(db.String(8), nullable=False)          # last 4 characters, for identification
    label = db.Column(db.String(160))                        # e.g. "Microwave Lab technicians"
    max_uses = db.Column(db.Integer, nullable=False, default=1)
    uses = db.Column(db.Integer, nullable=False, default=0)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked = db.Column(db.Boolean, nullable=False, default=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def state(self):
        if self.revoked:
            return "Revoked"
        if self.uses >= self.max_uses:
            return "Used up"
        if self.expires_at <= utcnow():
            return "Expired"
        return "Active"


class ConditionLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    equipment_id = db.Column(db.Integer, db.ForeignKey("equipment.id", ondelete="CASCADE"), nullable=False)
    old_condition = db.Column(db.String(120))   # unit summary before, e.g. "4 functional · 1 damaged"
    new_condition = db.Column(db.String(120), nullable=False)
    changed_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    changed_at = db.Column(db.DateTime, default=utcnow)

    changed_by = db.relationship("User")
    equipment = db.relationship("Equipment")
