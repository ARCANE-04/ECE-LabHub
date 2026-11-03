"""End-to-end tests for the IIT Roorkee Lab Equipment Portal.

Run:  python -m pytest -q
"""
import re
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import openpyxl
import pytest

from portal import create_app
from portal.auth import generate_access_code
from portal.extensions import db
from portal.models import (Booking, Equipment, Issue, Lab, StaffAccessCode, TimetableEntry, User,
                           utcnow)
from portal.services import inventory_stats


def now_local():
    return datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None, microsecond=0)


# ------------------------------------------------------------------ fixtures & helpers
@pytest.fixture()
def app(tmp_path):
    outbox = []
    app = create_app({
        "TESTING": True, "SECRET_KEY": "test-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path/'test.db'}",
        "WTF_CSRF_ENABLED": False, "_TEST_OUTBOX": outbox,
        "BOOKING_DAY_START": "00:00", "BOOKING_DAY_END": "23:59",
        "BOOKING_BREAKS": [], "BOOKING_WEEKDAYS": [0, 1, 2, 3, 4, 5, 6],
    })
    app.config["OUTBOX"] = outbox
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def last_link(app, kind):
    body = app.config["OUTBOX"][-1]["body"]
    return re.search(r"http://localhost(/account/%s/\S+)" % kind, body).group(1)


def register(client, email="asha_k@ece.iitr.ac.in", pw="SecurePass123", name="Asha Kumar"):
    return client.post("/student/register", data={"full_name": name, "email": email, "enrollment_no": "21116001",
                                                  "department": "ECE", "password": pw, "password2": pw})


def verified_student(app, client, email="asha_k@ece.iitr.ac.in", pw="SecurePass123"):
    register(client, email, pw)
    client.get(last_link(app, "verify"))
    r = client.post("/student/login", data={"email": email, "password": pw})
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    return email


def make_staff(app, email="labadmin@iitr.ac.in", pw="StaffPass123", admin=True):
    with app.app_context():
        u = User(email=email, full_name="Lab Admin", role="staff", is_admin=admin, email_verified_at=utcnow())
        u.set_password(pw)
        db.session.add(u)
        db.session.commit()
    return email, pw


def staff_login(app, client, admin=True, email="labadmin@iitr.ac.in"):
    email, pw = make_staff(app, email=email, admin=admin)
    r = client.post("/staff/login", data={"email": email, "password": pw})
    assert r.status_code == 302 and r.headers["Location"].endswith("/staff/")


def eq_id(app, name, units=None, **kw):
    """Find equipment by name; optionally set (functional, partial, damaged) unit counts."""
    with app.app_context():
        eq = Equipment.query.filter(Equipment.name == name, *(getattr(Equipment, k) == v for k, v in kw.items())).first()
        if units is not None:
            eq.set_units(*units)
            db.session.commit()
        return eq.id


def all_working(app, name, **kw):
    with app.app_context():
        q = Equipment.query.filter(Equipment.name == name, *(getattr(Equipment, k) == v for k, v in kw.items())).first().quantity
    return eq_id(app, name, units=(q, 0, 0), **kw)


def slot(days=1, start="10:00", end="11:00"):
    return {"date": (now_local() + timedelta(days=days)).date().isoformat(), "start": start, "end": end}


def staff_register(client, code, email="tech1@iitr.ac.in", pw="TechPass1234"):
    return client.post("/staff/register", data={"access_code": code, "full_name": "Ravi Tech", "email": email,
                                                 "designation": "Lab Technician", "department": "ECE",
                                                 "password": pw, "password2": pw})


# ------------------------------------------------------------------ data import
def test_inventory_matches_source_files(app):
    ws = openpyxl.load_workbook("data/source/instrument DEL Lab.xlsx").active
    del_units = sum(sum(int(x) for x in str(d).split("+"))
                    for (d,) in ws.iter_rows(min_col=4, max_col=4, values_only=True) if d is not None)
    with app.app_context():
        labs = {l.code: l for l in Lab.query.all()}
        assert {l.name for l in labs.values()} == {"DEL Lab", "Microwave Lab", "Prof. B. P. Das Inventory",
                                                   "Solid State Laboratory"}
        assert all(l.room_number is None for l in labs.values())
        assert sum(e.quantity for e in labs["del-lab"].equipment) == del_units == 449
        assert sum(e.quantity for e in labs["microwave-lab"].equipment) == 26
        assert sum(e.quantity for e in labs["prof-bp-das"].equipment) == 272
        ssl_ws = openpyxl.load_workbook("data/source/Solid State Lab eqp_list_prep_sumit.xlsx").active
        ssl_rows = [r for r in ssl_ws.iter_rows(min_row=2, values_only=True) if r[0] is not None]
        assert len(labs["solid-state-lab"].equipment) == len(ssl_rows) == 55
        assert sum(e.quantity for e in labs["solid-state-lab"].equipment) == sum(r[5] for r in ssl_rows) == 62
        assert TimetableEntry.query.count() == 0
        assert {e.condition for e in Equipment.query} == {"Unknown"}
        assert Equipment.query.filter_by(access_level="Research only").count() == 4


def test_stats_units_and_records(app):
    with app.app_context():
        s = inventory_stats()
        assert s["records"] == 161 and s["quantity"] == 809
        assert s["units"] == {"Functional": 0, "Partially Functional": 0, "Damaged": 0, "Unknown": 809}
        eq = Equipment.query.filter_by(name="Raspberry Pi 3").first()   # 4 units
        eq.set_units(2, 1, 1)
        db.session.commit()
        s = inventory_stats()
        assert s["units"] == {"Functional": 2, "Partially Functional": 1, "Damaged": 1, "Unknown": 805}
        assert s["by_status"]["Mixed"] == {"records": 1, "quantity": 4}
        assert s["records_with_damage"] == 1


def test_derived_record_status(app):
    with app.app_context():
        eq = Equipment.query.filter_by(name="Raspberry Pi 3").first()
        for units, expected in [((4, 0, 0), "Functional"), ((0, 0, 4), "Damaged"), ((0, 4, 0), "Partially Functional"),
                                ((3, 0, 1), "Mixed"), ((2, 0, 0), "Mixed"), ((0, 0, 0), "Unknown")]:
            eq.set_units(*units)
            assert eq.condition == expected, units
        with pytest.raises(ValueError):
            eq.set_units(3, 1, 1)   # 5 > 4 units


# ------------------------------------------------------------------ gateway & access
def test_gateway_is_first_page_and_portal_requires_sign_in(client):
    html = client.get("/").get_data(as_text=True)
    assert "Sign in to continue" in html and "Register with access code" in html and "Create student account" in html
    for url in ["/labs", "/equipment", "/equipment/1", "/timetable", "/about", "/bookings"]:
        r = client.get(url)
        assert r.status_code == 302 and "/student/login" in r.headers["Location"], url
    assert client.get("/api/search?q=osc").status_code == 401


@pytest.mark.parametrize("url", ["/student/login", "/student/register", "/staff/login", "/staff/register",
                                 "/account/forgot-password", "/account/resend-verification"])
def test_auth_pages_render(client, url):
    assert client.get(url).status_code == 200


@pytest.mark.parametrize("url", ["/", "/labs", "/labs/del-lab", "/labs/microwave-lab", "/labs/solid-state-lab", "/equipment",
                                 "/equipment?q=oscilloscope", "/equipment?condition=Unknown&bookable=1",
                                 "/equipment/1", "/timetable", "/about", "/profile", "/bookings"])
def test_student_pages_render(app, client, url):
    verified_student(app, client)
    assert client.get(url).status_code == 200


def test_search(app, client):
    verified_student(app, client)
    assert "Vector Network Analyzer" in client.get("/equipment?q=s-parameters").get_data(as_text=True)
    assert any("Tektronix" in r["name"] for r in client.get("/api/search?q=tektronix").get_json())


# ------------------------------------------------------------------ student auth
def test_non_institutional_email_rejected(app, client):
    for bad in ["someone@gmail.com", "x@iitr.ac.in.evil.com", "x@notiitr.ac.in"]:
        assert "institutional email" in register(client, email=bad).get_data(as_text=True)
    with app.app_context():
        assert User.query.count() == 0
    assert app.config["OUTBOX"] == []


def test_registration_requires_email_verification(app, client):
    register(client)
    with app.app_context():
        u = User.query.filter_by(email="asha_k@ece.iitr.ac.in").one()
        assert u.password_hash.startswith(("scrypt", "pbkdf2")) and not u.is_verified
    r = client.post("/student/login", data={"email": "asha_k@ece.iitr.ac.in", "password": "SecurePass123"})
    assert r.status_code == 200 and "not verified" in r.get_data(as_text=True)
    link = last_link(app, "verify")
    assert client.get(link).status_code == 302
    client.get(link)   # second use is harmless
    assert client.post("/student/login", data={"email": "asha_k@ece.iitr.ac.in", "password": "SecurePass123"}).status_code == 302


def test_tampered_verification_token_rejected(app, client):
    register(client)
    client.get(last_link(app, "verify")[:-3] + "abc")
    with app.app_context():
        assert not User.query.one().is_verified


def test_password_reset_flow(app, client):
    verified_student(app, client)
    client.post("/logout")
    client.post("/account/forgot-password", data={"email": "asha_k@ece.iitr.ac.in"})
    link = last_link(app, "reset-password")
    assert client.post(link, data={"password": "BrandNewPass99", "password2": "BrandNewPass99"}).status_code == 302
    client.post(link, data={"password": "Another1234x", "password2": "Another1234x"})   # single-use
    assert client.post("/student/login", data={"email": "asha_k@ece.iitr.ac.in", "password": "Another1234x"}).status_code == 200
    assert client.post("/student/login", data={"email": "asha_k@ece.iitr.ac.in", "password": "BrandNewPass99"}).status_code == 302


def test_account_lockout(app, client):
    register(client)
    client.get(last_link(app, "verify"))
    for _ in range(5):
        client.post("/student/login", data={"email": "asha_k@ece.iitr.ac.in", "password": "wrong"})
    r = client.post("/student/login", data={"email": "asha_k@ece.iitr.ac.in", "password": "SecurePass123"})
    assert "Too many failed attempts" in r.get_data(as_text=True)


# ------------------------------------------------------------------ staff sign-up with access code
def test_staff_registration_requires_valid_code(app, client):
    r = staff_register(client, "AAAA-BBBB-CCCC")
    assert "invalid, expired or already used" in r.get_data(as_text=True)
    with app.app_context():
        plain, code = generate_access_code("Test", 1, 7)
        assert plain not in (code.code_hash, code.hint) and len(code.code_hash) == 64
    r = staff_register(client, plain.lower().replace("-", " "))   # tolerant of case/spacing
    assert "Check your inbox" in r.get_data(as_text=True)
    with app.app_context():
        u = User.query.filter_by(email="tech1@iitr.ac.in").one()
        assert u.role == "staff" and not u.is_admin and not u.is_verified
        assert StaffAccessCode.query.one().uses == 1
    # cannot sign in before verifying
    r = client.post("/staff/login", data={"email": "tech1@iitr.ac.in", "password": "TechPass1234"})
    assert "not verified" in r.get_data(as_text=True)
    client.get(last_link(app, "verify"))
    r = client.post("/staff/login", data={"email": "tech1@iitr.ac.in", "password": "TechPass1234"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/staff/")
    client.post("/logout")
    # single-use code cannot be reused
    r = staff_register(client, plain, email="tech2@iitr.ac.in")
    assert "invalid, expired or already used" in r.get_data(as_text=True)


def test_staff_code_expiry_revocation_and_domain(app, client):
    with app.app_context():
        plain, code = generate_access_code("x", 5, 7)
        code.expires_at = utcnow() - timedelta(minutes=1)
        db.session.commit()
    assert "invalid, expired" in staff_register(client, plain).get_data(as_text=True)
    with app.app_context():
        plain2, code2 = generate_access_code("y", 5, 7)
        code2.revoked = True
        db.session.commit()
    assert "invalid, expired" in staff_register(client, plain2).get_data(as_text=True)
    with app.app_context():
        plain3, _ = generate_access_code("z", 5, 7)
    assert "institutional email" in staff_register(client, plain3, email="tech@gmail.com").get_data(as_text=True)
    with app.app_context():
        assert User.query.count() == 0


def test_admin_generates_and_revokes_codes(app, client):
    staff_login(app, client)
    r = client.post("/staff/access", data={"action": "generate", "label": "MW tech", "max_uses": 2, "days": 7})
    plain = re.search(r'id="newCode">([A-Z0-9-]+)<', r.get_data(as_text=True)).group(1)
    with app.app_context():
        code = StaffAccessCode.query.one()
        assert code.max_uses == 2 and code.label == "MW tech"
        cid = code.id
    assert plain not in client.get("/staff/access").get_data(as_text=True)   # shown once only
    client.post("/staff/access", data={"action": "revoke", "code_id": cid})
    client.post("/logout")
    assert "invalid, expired" in staff_register(client, plain).get_data(as_text=True)


def test_non_admin_staff_cannot_manage_access(app, client):
    staff_login(app, client, admin=False, email="plainstaff@iitr.ac.in")
    assert client.get("/staff/access").status_code == 403
    assert client.post("/staff/access", data={"action": "generate", "max_uses": 1, "days": 1}).status_code == 403
    assert client.get("/staff/inventory").status_code == 200


def test_admin_can_disable_staff(app, client):
    make_staff(app, email="other@iitr.ac.in", admin=False)
    other = app.test_client()
    other.post("/staff/login", data={"email": "other@iitr.ac.in", "password": "StaffPass123"})
    assert other.get("/staff/").status_code == 200
    staff_login(app, client)
    with app.app_context():
        uid = User.query.filter_by(email="other@iitr.ac.in").one().id
    client.post("/staff/access", data={"action": "disable", "user_id": uid})
    assert other.get("/staff/").status_code == 302   # session ended


# ------------------------------------------------------------------ role separation
def test_staff_routes_protected(app, client):
    for url in ["/staff/", "/staff/inventory", "/staff/issues", "/staff/timetable", "/staff/labs", "/staff/bookings", "/staff/access"]:
        r = client.get(url)
        assert r.status_code == 302 and "/staff/login" in r.headers["Location"]
    verified_student(app, client)
    for url in ["/staff/", "/staff/inventory", "/staff/issues", "/staff/timetable", "/staff/access"]:
        assert client.get(url).status_code == 403
    assert client.post("/staff/inventory/1/units", data={"quantity": 5, "functional": 0, "partial": 0, "damaged": 5}).status_code == 403


def test_roles_cannot_cross_login(app, client):
    verified_student(app, client)
    client.post("/logout")
    r = client.post("/staff/login", data={"email": "asha_k@ece.iitr.ac.in", "password": "SecurePass123"})
    assert r.status_code == 200 and "Incorrect" in r.get_data(as_text=True)
    email, pw = make_staff(app)
    assert client.post("/student/login", data={"email": email, "password": pw}).status_code == 200


def test_staff_cannot_book_and_anonymous_cannot_act(app, client):
    eid = all_working(app, "Arduino Due")
    r = client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 1})
    assert r.status_code == 302 and "/student/login" in r.headers["Location"]
    r = client.post(f"/equipment/{eid}/report-issue", data={"title": "Broken", "description": "Does not power on"})
    assert r.status_code == 302 and "/student/login" in r.headers["Location"]
    staff_login(app, client)
    assert client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 1}).status_code == 403


# ------------------------------------------------------------------ booking
def test_booking_confirmed_when_inspected_units_cover_it(app, client):
    verified_student(app, client)
    eid = all_working(app, "Raspberry Pi 3")
    r = client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 2, "purpose": "IoT project"})
    assert r.status_code == 302 and "/bookings/LB-" in r.headers["Location"]
    with app.app_context():
        b = Booking.query.one()
        assert b.status == "Confirmed" and re.fullmatch(r"LB-\d{4}-[A-Z0-9]{6}", b.reference)
        ref = b.reference
    assert "Booking confirmed" in client.get(f"/bookings/{ref}?new=1").get_data(as_text=True)
    assert ref in client.get("/bookings").get_data(as_text=True)


def test_uninspected_units_give_pending_until_staff_confirm(app, client):
    verified_student(app, client)
    eid = eq_id(app, "Raspberry Pi 3", units=(1, 0, 0))   # 1 working, 3 uninspected
    client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 1})
    client.post(f"/equipment/{eid}/book", data={**slot(start="12:00", end="13:00"), "quantity": 2})
    with app.app_context():
        statuses = sorted(b.status for b in Booking.query)
        assert statuses == ["Confirmed", "Pending"]
        ref = Booking.query.filter_by(status="Pending").one().reference
    client.post("/logout")
    staff_login(app, client)
    client.post(f"/staff/bookings/{ref}/decide", data={"action": "confirm"})
    with app.app_context():
        assert Booking.query.filter_by(reference=ref).one().status == "Confirmed"


def test_damaged_units_reduce_capacity(app, client):
    verified_student(app, client)
    eid = eq_id(app, "Raspberry Pi 3", units=(1, 0, 3))   # 4 in stock, 3 damaged -> 1 usable
    d = client.get(f"/api/equipment/{eid}/availability", query_string=slot()).get_json()
    assert d["usable"] == 1 and d["available"] == 1
    r = client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 2}, follow_redirects=True)
    assert "Only 1 unit" in r.get_data(as_text=True)
    assert client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 1}).status_code == 302


def test_all_damaged_research_and_zero_quantity_not_bookable(app, client):
    verified_student(app, client)
    dmg = eq_id(app, "Arduino Due", units=(0, 0, 5))
    assert "All units are marked damaged" in client.post(f"/equipment/{dmg}/book", data={**slot(), "quantity": 1},
                                                         follow_redirects=True).get_data(as_text=True)
    res = all_working(app, "Vector Network Analyzer", manufacturer="Anritsu")
    assert "research use" in client.post(f"/equipment/{res}/book", data={**slot(), "quantity": 1},
                                         follow_redirects=True).get_data(as_text=True)
    with app.app_context():
        e = Equipment.query.filter_by(name="Arduino Nano 33").first()
        e.quantity = 0
        e.set_units(0, 0, 0)
        db.session.commit()
        zero = e.id
    assert "No units in inventory" in client.post(f"/equipment/{zero}/book", data={**slot(), "quantity": 1},
                                                  follow_redirects=True).get_data(as_text=True)
    with app.app_context():
        assert Booking.query.count() == 0


def test_overbooking_and_double_booking_prevented(app, client):
    verified_student(app, client)
    eid = all_working(app, "Raspberry Pi 3")   # 4 units
    assert client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 3}).status_code == 302
    r = client.post(f"/equipment/{eid}/book", data={**slot(start="10:30", end="11:30"), "quantity": 1}, follow_redirects=True)
    assert "already have booking" in r.get_data(as_text=True)
    client.post("/logout")
    verified_student(app, client, email="ravi@me.iitr.ac.in")
    r = client.post(f"/equipment/{eid}/book", data={**slot(start="10:30", end="11:30"), "quantity": 2}, follow_redirects=True)
    assert "Only 1 unit" in r.get_data(as_text=True)
    assert client.post(f"/equipment/{eid}/book", data={**slot(start="10:30", end="11:30"), "quantity": 1}).status_code == 302
    assert client.post(f"/equipment/{eid}/book", data={**slot(start="12:00", end="13:00"), "quantity": 4}).status_code == 302
    with app.app_context():
        assert Booking.query.filter(Booking.status.in_(("Pending", "Confirmed"))).count() == 3


def test_peak_load_not_overcounted(app, client):
    verified_student(app, client)
    eid = all_working(app, "Raspberry Pi 3")
    client.post(f"/equipment/{eid}/book", data={**slot(start="10:00", end="11:00"), "quantity": 1})
    client.post("/logout")
    verified_student(app, client, email="ravi@me.iitr.ac.in")
    client.post(f"/equipment/{eid}/book", data={**slot(start="11:00", end="12:00"), "quantity": 1})
    assert client.get(f"/api/equipment/{eid}/availability", query_string=slot(start="10:00", end="12:00")).get_json()["available"] == 3


def test_invalid_windows_rejected(app, client):
    verified_student(app, client)
    eid = all_working(app, "Raspberry Pi 3")
    cases = [({**slot(), "start": "11:00", "end": "10:00"}, "End time must be after"),
             (slot(days=-1), "must start in the future"),
             (slot(days=40), "days in advance"),
             (slot(start="09:00", end="15:00"), "Maximum booking length"),
             ({**slot(), "quantity": 0}, "at least 1")]
    for data, msg in cases:
        data = {"quantity": 1, **data}
        assert msg in client.post(f"/equipment/{eid}/book", data=data, follow_redirects=True).get_data(as_text=True), msg
    with app.app_context():
        assert Booking.query.count() == 0


def test_timetable_class_blocks_booking(app, client):
    eid = all_working(app, "Raspberry Pi 3")
    target = now_local() + timedelta(days=1)
    with app.app_context():
        lab = db.session.get(Equipment, eid).lab
        db.session.add(TimetableEntry(lab_id=lab.id, day_of_week=target.weekday(),
                                      start_time=target.replace(hour=9, minute=0).time(),
                                      end_time=target.replace(hour=12, minute=0).time(),
                                      activity_type="Class", title="Test class", verified_source="test"))
        db.session.commit()
    verified_student(app, client)
    assert "class slot" in client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 1},
                                       follow_redirects=True).get_data(as_text=True)
    assert client.post(f"/equipment/{eid}/book", data={**slot(start="13:00", end="14:00"), "quantity": 1}).status_code == 302


def test_cancellation_and_history(app, client):
    verified_student(app, client)
    eid = all_working(app, "DragonBoard 410C")   # 2 units
    client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 2})
    with app.app_context():
        ref = Booking.query.one().reference
    assert client.get(f"/api/equipment/{eid}/availability", query_string=slot()).get_json()["available"] == 0
    client.post(f"/bookings/{ref}/cancel")
    assert client.get(f"/api/equipment/{eid}/availability", query_string=slot()).get_json()["available"] == 2
    assert ref in client.get("/bookings").get_data(as_text=True)


def test_student_cannot_touch_others_booking(app, client):
    verified_student(app, client)
    eid = all_working(app, "DragonBoard 410C")
    client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 1})
    with app.app_context():
        ref = Booking.query.one().reference
    client.post("/logout")
    verified_student(app, client, email="ravi@me.iitr.ac.in")
    assert client.get(f"/bookings/{ref}").status_code == 404
    assert client.post(f"/bookings/{ref}/cancel").status_code == 404


def test_past_bookings_completed(app, client):
    verified_student(app, client)
    eid = all_working(app, "DragonBoard 410C")
    client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 1})
    with app.app_context():
        b = Booking.query.one()
        b.booking_date = (now_local() - timedelta(days=2)).date()
        db.session.commit()
    client.get("/bookings")
    with app.app_context():
        assert Booking.query.one().status == "Completed"


def test_concurrent_bookings_never_overbook(app):
    eid = all_working(app, "DragonBoard 410C")
    emails = [f"student{i}@ece.iitr.ac.in" for i in range(10)]
    with app.app_context():
        for e in emails:
            u = User(email=e, full_name=e, role="student", email_verified_at=utcnow())
            u.set_password("SecurePass123")
            db.session.add(u)
        db.session.commit()
    clients = []
    for e in emails:
        c = app.test_client()
        assert c.post("/student/login", data={"email": e, "password": "SecurePass123"}).status_code == 302
        clients.append(c)
    barrier, results = threading.Barrier(len(clients)), []

    def go(c):
        barrier.wait()
        results.append(c.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 1}).headers.get("Location", ""))

    threads = [threading.Thread(target=go, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with app.app_context():
        active = Booking.query.filter(Booking.equipment_id == eid, Booking.status.in_(("Pending", "Confirmed"))).all()
        assert sum(b.quantity for b in active) == 2
    assert sum("/bookings/LB-" in r for r in results) == 2


# ------------------------------------------------------------------ staff unit management
def test_staff_sets_unit_counts_and_stats_follow(app, client):
    staff_login(app, client)
    eid = eq_id(app, "Raspberry Pi 3")
    r = client.post(f"/staff/inventory/{eid}/units", data={"quantity": 4, "functional": 2, "partial": 1, "damaged": 1},
                    follow_redirects=True)
    assert "2 working · 1 partial · 1 damaged" in r.get_data(as_text=True)
    with app.app_context():
        e = db.session.get(Equipment, eid)
        assert (e.units_functional, e.units_partial, e.units_damaged, e.units_unknown) == (2, 1, 1, 0)
        assert e.condition == "Mixed"
        assert inventory_stats()["units"]["Damaged"] == 1
    html = client.get("/staff/").get_data(as_text=True)
    assert 'data-stat="u-damaged">1<' in html and 'data-stat="quantity">809<' in html
    assert "1 damaged" in client.get("/staff/inventory?has=damaged").get_data(as_text=True)


def test_unit_counts_validated(app, client):
    staff_login(app, client)
    eid = eq_id(app, "Raspberry Pi 3")
    for data in [{"quantity": 4, "functional": 3, "partial": 1, "damaged": 1},
                 {"quantity": 4, "functional": -1, "partial": 0, "damaged": 0},
                 {"quantity": 4, "functional": "x", "partial": 0, "damaged": 0}]:
        client.post(f"/staff/inventory/{eid}/units", data=data)
    with app.app_context():
        e = db.session.get(Equipment, eid)
        assert (e.units_functional, e.units_partial, e.units_damaged) == (0, 0, 0)


def test_quick_mark_all_damaged_and_stock_change(app, client):
    staff_login(app, client)
    eid = eq_id(app, "Arduino Due")
    client.post(f"/staff/inventory/{eid}/units", data={"mode": "all", "condition": "Damaged"})
    with app.app_context():
        e = db.session.get(Equipment, eid)
        assert e.units_damaged == e.quantity == 5 and e.condition == "Damaged"
    client.post(f"/staff/inventory/{eid}/units", data={"quantity": 7, "functional": 6, "partial": 0, "damaged": 1})
    with app.app_context():
        e = db.session.get(Equipment, eid)
        assert e.quantity == 7 and e.units_functional == 6 and inventory_stats()["quantity"] == 811


def test_marking_units_damaged_cancels_only_bookings_that_no_longer_fit(app, client):
    verified_student(app, client)
    eid = all_working(app, "Raspberry Pi 3")   # 4 units
    client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 2})
    client.post("/logout")
    verified_student(app, client, email="ravi@me.iitr.ac.in")
    client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 2})
    client.post(f"/equipment/{eid}/book", data={**slot(start="15:00", end="16:00"), "quantity": 1})
    client.post("/logout")
    staff_login(app, client)
    client.post(f"/staff/inventory/{eid}/units", data={"quantity": 4, "functional": 3, "partial": 0, "damaged": 1})
    with app.app_context():
        rows = Booking.query.order_by(Booking.id).all()
        assert [b.status for b in rows] == ["Confirmed", "Cancelled", "Confirmed"]   # first come, first kept


def test_cannot_reduce_stock_below_booked(app, client):
    verified_student(app, client)
    eid = all_working(app, "Raspberry Pi 3")
    client.post(f"/equipment/{eid}/book", data={**slot(), "quantity": 3})
    client.post("/logout")
    staff_login(app, client)
    r = client.post(f"/staff/inventory/{eid}/units", data={"quantity": 2, "functional": 2, "partial": 0, "damaged": 0},
                    follow_redirects=True)
    assert "Cannot reduce stock" in r.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Equipment, eid).quantity == 4


def test_add_equipment_with_units(app, client):
    staff_login(app, client)
    with app.app_context():
        lab_id = Lab.query.filter_by(code="del-lab").one().id
    r = client.post("/staff/inventory/new", data={"name": "Logic Analyzer", "lab_id": lab_id, "category": "Pattern Generator",
                                                  "quantity": 6, "functional": 4, "partial": 0, "damaged": 2,
                                                  "access_level": "UG/PG Labs & Research", "bookable": "1"})
    assert r.status_code == 302
    with app.app_context():
        e = Equipment.query.filter_by(name="Logic Analyzer").one()
        assert (e.quantity, e.units_functional, e.units_damaged, e.condition) == (6, 4, 2, "Mixed")
        assert inventory_stats()["records"] == 162


def test_staff_edits_lab_room(app, client):
    staff_login(app, client)
    with app.app_context():
        lab = Lab.query.filter_by(code="del-lab").one()
    client.post("/staff/labs", data={"lab_id": lab.id, "name": "DEL Lab", "room_number": "TEST-101",
                                     "description": "x", "booking_enabled": "1"})
    client.post("/logout")
    verified_student(app, client)
    assert "Room TEST-101" in client.get("/labs/del-lab").get_data(as_text=True)


def test_timetable_and_issue_management(app, client):
    verified_student(app, client)
    client.post("/equipment/1/report-issue", data={"title": "No trace on CH1", "description": "Channel 1 shows nothing", "severity": "High"})
    client.post("/logout")
    staff_login(app, client)
    with app.app_context():
        lab_id = Lab.query.filter_by(code="del-lab").one().id
    client.post("/staff/timetable", data={"lab_id": lab_id, "days": ["0", "2"], "start": "14:00", "end": "17:00",
                                          "activity_type": "Class", "title": "Lab session", "verified_source": "Unit test"})
    client.post("/staff/timetable", data={"lab_id": lab_id, "days": ["0"], "start": "15:00", "end": "16:00",
                                          "activity_type": "Reserved", "verified_source": "Unit test"})
    with app.app_context():
        assert TimetableEntry.query.count() == 2
        iid = Issue.query.one().id
    assert "No trace on CH1" in client.get("/staff/issues").get_data(as_text=True)
    client.post("/staff/issues", data={"issue_id": iid, "status": "Resolved", "staff_note": "Probe replaced"})
    with app.app_context():
        assert Issue.query.one().status == "Resolved"


def test_csrf_enforced_when_enabled(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "x", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path/'c.db'}"})
    assert app.test_client().post("/student/login", data={"email": "a@iitr.ac.in", "password": "x"}).status_code == 400


# ------------------------------------------------------------ booking-hours rules
def _next_weekday(target):
    d = (now_local() + timedelta(days=1)).date()
    while d.weekday() != target:
        d += timedelta(days=1)
    return d.isoformat()


def test_booking_hours_lunch_and_weekends_enforced(app, client):
    app.config.update(BOOKING_DAY_START="09:00", BOOKING_DAY_END="17:30",
                      BOOKING_BREAKS=["13:00-14:00"], BOOKING_WEEKDAYS=[0, 1, 2, 3, 4])
    verified_student(app, client)
    with app.app_context():
        eq = Equipment.query.filter(Equipment.access_level != "Research only", Equipment.bookable.is_(True),
                                    Equipment.quantity > 0).first()
        eq.set_units(eq.quantity, 0, 0)
        db.session.commit()
        eid = eq.id
        lab = eq.lab
        lab.booking_enabled = True
        TimetableEntry.query.filter_by(lab_id=lab.id).delete()
        db.session.commit()
    mon, sat, sun = _next_weekday(0), _next_weekday(5), _next_weekday(6)
    cases = [
        ({"date": mon, "start": "08:30", "end": "09:30"}, "between 09:00 and 17:30"),
        ({"date": mon, "start": "17:00", "end": "18:00"}, "between 09:00 and 17:30"),
        ({"date": mon, "start": "12:30", "end": "13:30"}, "lunch"),
        ({"date": mon, "start": "13:00", "end": "14:00"}, "lunch"),
        ({"date": sat, "start": "10:00", "end": "11:00"}, "Saturday"),
        ({"date": sun, "start": "10:00", "end": "11:00"}, "Sunday"),
    ]
    for data, msg in cases:
        r = client.post(f"/equipment/{eid}/book", data={**data, "quantity": 1}, follow_redirects=True)
        assert msg in r.get_data(as_text=True), (data, msg)
        a = client.get(f"/api/equipment/{eid}/availability", query_string=data)
        assert a.status_code == 400 and msg in a.get_json()["error"]
    with app.app_context():
        assert Booking.query.count() == 0
    for data in ({"date": mon, "start": "09:00", "end": "10:00"}, {"date": mon, "start": "14:00", "end": "15:00"},
                 {"date": mon, "start": "16:30", "end": "17:30"}):
        r = client.post(f"/equipment/{eid}/book", data={**data, "quantity": 1})
        assert r.status_code == 302 and "/bookings/" in r.headers["Location"], data
    page = client.get(f"/equipment/{eid}").get_data(as_text=True)
    assert "lab-timetable" in page and "timetable" in page.lower()


def test_staff_timetable_clock_defaults_to_zero(app, client):
    staff_login(app, client)
    page = client.get("/staff/timetable").get_data(as_text=True)
    assert page.count('value="00:00"') >= 2 and "data-clock" in page


def test_new_lab_added_to_existing_database_without_losing_data(app, client):
    """A lab added to LABS later is imported on the next start; existing users stay."""
    verified_student(app, client)
    with app.app_context():
        lab = Lab.query.filter_by(code="solid-state-lab").first()
        db.session.delete(lab)
        db.session.commit()
        assert Lab.query.filter_by(code="solid-state-lab").count() == 0
    app2 = create_app({"TESTING": True, "SECRET_KEY": "x", "SQLALCHEMY_DATABASE_URI": app.config["SQLALCHEMY_DATABASE_URI"],
                       "WTF_CSRF_ENABLED": False, "_TEST_OUTBOX": []})
    with app2.app_context():
        lab = Lab.query.filter_by(code="solid-state-lab").one()
        assert sum(e.quantity for e in lab.equipment) == 62
        assert User.query.filter_by(email="asha_k@ece.iitr.ac.in").count() == 1
        assert Lab.query.count() == 4
