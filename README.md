# IIT Roorkee Lab Equipment Discovery & Booking Portal

Flask · SQLite · SQLAlchemy · HTML/CSS/JS. Students discover equipment, check laboratory
accessibility and book units with a verified `@iitr.ac.in` account; staff manage inventory,
condition, issues, bookings and timetables.

## Quick start

```bash
pip install -r requirements.txt
flask --app portal create-staff          # first administrator: prompts for email, name, password
python run.py                            # http://127.0.0.1:5000
python -m pytest -q                      # 57 end-to-end tests
```

The site opens on a **sign-in / sign-up gateway** for students and staff; every other page needs
an account.

* **Students** create an account with their `@iitr.ac.in` (or department sub-domain) address and
  must open the emailed verification link before signing in.
* **Staff** create an account at *Staff → Create account* with an institutional address **and a
  staff access code**. Administrators issue codes in *Staff console → Staff access* (or
  `flask --app portal create-staff-code --label "MW Lab tech" --uses 1 --days 7`). Each code is
  shown once, stored only as a keyed hash, expires, has a use limit and can be revoked. Staff also
  verify their email. Administrators can disable staff accounts or grant admin rights.

> Upgrading from the first version? The database schema changed (unit-level condition). Delete
> `instance/labportal.db` or run `flask --app portal reset-data`.

The database (`instance/labportal.db`) is created and filled from `data/source/` on first start. A lab added to `LABS` in `portal/seed_data.py` later is imported automatically on the next start, without touching existing users or bookings.
`flask --app portal stats` prints live inventory figures; `flask --app portal reset-data` rebuilds
the database from the source files (deletes users and bookings).

## Configuration (environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `SECRET_KEY` | Session / token signing key | generated into `instance/secret_key` |
| `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_USE_TLS`, `MAIL_SENDER` | SMTP for verification and password-reset emails | unset → emails saved to `instance/outbox/*.eml` (dev only) |
| `STUDENT_EMAIL_DOMAINS` | Allowed student domains (sub-domains included) | `iitr.ac.in` |
| `STAFF_EMAIL_DOMAINS` | Allowed staff domains | `iitr.ac.in` |
| `BOOKING_DAY_START` / `BOOKING_DAY_END` | Bookable hours | `08:00` / `20:00` |
| `BOOKING_MAX_UNITS` | Max units per booking | `5` |
| `SESSION_COOKIE_SECURE` | Set `1` behind HTTPS | `0` |
| `DATABASE_URL` | SQLAlchemy URL | SQLite in `instance/` |

**For production you must configure SMTP** — otherwise verification links only reach the
server's outbox folder, not the student.

## Data provenance — what was and wasn't in the source files

| Laboratory (as named in source) | Source | Records | Units |
|---|---|---|---|
| DEL Lab | `instrument DEL Lab.xlsx` | 67 | 449 |
| Microwave Lab | `MW_Lab_Data.pdf` (scanned) → `MW_Lab_Data_transcribed.csv` | 16 | 26 |
| Prof. B. P. Das Inventory | `Prof_BP_Das_inventory.csv` | 23 | 272 |
| Solid State Laboratory | `Solid State Lab eqp_list_prep_sumit.xlsx` (table T-01…T-13 / rack kept in notes) | 55 | 62 |
| **Total** | | **161** | **809** |

* Every record stores its source text and row reference (`source_text`, `source_ref`).
* DEL Lab: two-line merged cells were joined; the 18 single-unit "MDC-01" rows were combined into
  one record of 18; row 85 "5+5" was split into 5 TBS1072B DSOs + 5 AFG1022 generators. Column E
  (unlabeled) duplicates column D everywhere and is not used.
* Microwave Lab: items marked "For Research purpose" are stored as *Research only* and are not
  student-bookable.
* **Not in the sources, so not invented:** room numbers (shown as "Room not yet provided"),
  equipment condition (every unit starts as *Not inspected*), timetables (shown as "Schedule not provided"),
  full lab names (e.g. what "DEL" stands for). Staff enter these in the Staff Portal.

## Branding & images

* **Official IIT Roorkee logo:** place the institute's official file at
  `portal/static/img/iitr-logo.png` and it appears in every header and on login pages. It is not
  bundled; until then the header shows the institute name as text (no imitation emblem).
* The portal logo (`portal-logo.svg`) and all equipment illustrations are original artwork,
  one per equipment category, labelled "Representative illustration" in the UI.
* Staff can set a manufacturer/catalogue photo URL plus credit per item (Inventory → Edit). Only
  use images you have rights to. Broken images fall back to `fallback.svg`.

## Unit-level condition

Each equipment record stores how many of its units are **working**, **partially functional** and
**damaged**; the rest are **not inspected**. Staff set these in *Equipment & units* with the
*Update units* editor (steppers, live total check, quick "all working / all damaged" actions), or
on the item's edit page. The record's overall status is derived: Functional, Partially
Functional, Damaged, Not inspected, or **Mixed**. Dashboards report records and units separately
(a record with quantity 5 counts as 5 units), per condition and per laboratory, plus inspection
progress. Every change is logged with who made it.

## Booking rules (enforced on the server)

* Verified student only; staff and anonymous users cannot book.
* Damaged units are never bookable. Blocked entirely if every unit is damaged, the item is
  Research only, withdrawn by staff, has quantity 0, or the lab has booking disabled.
* Window: future, within bookable hours, 30 min – 4 h, ≤ 30 days ahead; must not overlap a Class
  or Reserved timetable slot of that lab.
* Capacity = (quantity − damaged units) − peak concurrent units already held in the window.
  The transaction takes SQLite's write lock before checking, so simultaneous requests are
  serialised (tested with 10 concurrent requests for 2 units).
* If inspected working/partial units cover the request it is **Confirmed** immediately; if it
  relies on uninspected units it is **Pending** until staff confirm. Past Confirmed →
  **Completed**; unconfirmed past Pending → **Cancelled**.
* When staff mark more units damaged, upcoming bookings are kept first-come-first-served and only
  those that no longer fit are cancelled. Stock cannot be reduced below what is already booked.

## Project layout

```
portal/__init__.py     app factory, config, CLI commands
portal/models.py       Lab, Equipment, Keyword, User, Booking, TimetableEntry, Issue, ConditionLog
portal/services.py     accessibility, availability, booking transaction, inventory statistics
portal/auth.py         gateway auth: student + staff sign-up/sign-in, access codes, verification, reset, RBAC
portal/main.py         public + student routes, JSON API
portal/staff.py        staff console (all routes require role=staff; access codes require admin)
portal/seed_data.py    source-file import with curation table
tests/test_portal.py   end-to-end tests
```
