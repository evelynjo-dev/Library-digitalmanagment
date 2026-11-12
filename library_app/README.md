# Smart Library - scan-based Library Book Manager (Design Studio I)

## Run on your laptop
1. Install Python 3.9+ from python.org (tick "Add to PATH").
2. Open a terminal in this folder and run:
       pip install -r requirements.txt
       python app.py
3. Open http://127.0.0.1:5000 (or http://localhost:5000) in your browser.
   The server listens on your computer only; it is not exposed to your network.

The first run creates an empty relational schema; it does not add sample records.
The previous SQLite schema is migrated automatically. Existing books, copies,
members and transactions are imported, and the original tables are retained as
`legacy_v1_*` recovery tables.

The database uses Flask-SQLAlchemy and includes users, books, book copies,
categories, racks, shelves, transactions, reservations, fines, notifications
and activity logs. Versioned migrations add student QR cards, password hashes,
and notification links without replacing existing records. Book availability
is derived from active transactions. Each copy and student has a distinct QR
payload. Labels include both QR and Code 128 barcodes.

## Secure account setup
There is no public signup or default password. Configure the initial
administrator before starting the app. In PowerShell, set the following values
in the same terminal used to start `python app.py`:

    $env:LIBRARY_SECRET_KEY = (python -c "import secrets; print(secrets.token_urlsafe(48))")
    $env:LIBRARY_ADMIN_ID = "LIB-ADMIN"
    $env:LIBRARY_ADMIN_NAME = "Library Administrator"
    $env:LIBRARY_ADMIN_PASSWORD = python -c "import getpass; print(getpass.getpass('Choose a password (12+ characters): '))"
    python app.py

The administrator ID must not already belong to a student. Passwords are
stored as secure hashes. After signing in, use Members to create student
records and assign each student a password. Existing students must be assigned
a password before their first sign-in. Keep `LIBRARY_SECRET_KEY` stable across
restarts; for HTTPS deployments set `LIBRARY_HTTPS=1` so the session cookie is
marked Secure. Do not commit credentials or secret keys.

## Pages
- Catalog (/)         : racks A-J, cover page, author, "x of y available", rack/row, search + category filter
- Book page           : copy-by-copy status; students can reserve titles
- Scan desk (/scan)   : administrator-only camera/manual QR issue and return
- Student portal (/student): profile, QR card, active loans, due dates, fines, reservation and borrowing history, notifications
- Dashboard (/dashboard): administrator-only live inventory and circulation analytics, including monthly issues/returns, popular titles, active students, category borrowing, overdue loans and availability
- Reports (/reports)  : administrator-only member borrowing, overdue and inventory reports in CSV and PDF
- Members             : administrator-only member management, passwords, and QR library cards
- Floor map (/map)     : searchable physical racks, shelves, categories, and live copy availability
- Labels (/labels)    : printable barcodes for every copy (needs internet once for the barcode script)

Reservations are ordered by reservation time. When copies become available,
the next eligible student is marked Ready and notified. A Ready reservation
has priority at issue time; duplicate active reservations are rejected.

Due-date and overdue notices are generated idempotently when a student visits
their portal or an administrator opens the dashboard. For a daily scheduled
run, use:

    flask --app app generate-due-notifications

## Reports and analytics
The dashboard aggregates the current SQLite records; it does not add synthetic
statistics. The analytics endpoint is available to administrators at
`/api/analytics`. Reports at `/reports` export student borrowing histories,
current overdue loans, and physical-copy inventory as CSV or PDF.

## Tests
Run the automated database and Flask integration tests from this folder:

    python -m unittest discover -s tests -v

The tests use temporary SQLite databases and do not modify the application
database.

## Data API

The management API accepts and returns JSON. `POST` creates, `PUT`/`PATCH`
updates, and `DELETE` removes records. Related records with transaction or
catalog history cannot be deleted.

- `/api/books` and `/api/books/<id>`: list, create, read, update and delete books
- `/api/books/<id>/copies`: list or add copies; `/api/copies/<copy_id>` reads or deletes a copy
- `/api/members` and `/api/members/<member_id>`: manage student members
- `/api/categories` and `/api/categories/<id>`: manage categories
- `/api/racks` and `/api/racks/<id>`: manage racks
- `/api/racks/<id>/shelves` and `/api/shelves/<id>`: manage shelves

The scan desk uses `html5-qrcode` for browser camera scanning and falls back to
manual ID/QR entry if camera access is unavailable. Student cards are available
at `/members/<member_id>/card` to their owner and administrators. The JSON
circulation endpoints are `POST /api/scan/lookup`, `POST /api/scan/issue`, and
`POST /api/scan/return`; they use the same SQLite-backed validation and
transactions as the form routes. Write APIs and circulation operations require
an administrator session and a valid CSRF token. Student reservations and
notification actions are limited to the authenticated student.

Validation errors return HTTP 400, missing API records 404, and uniqueness or
relationship conflicts 409. Unauthenticated APIs return 401 and forbidden
role access returns 403. Dashboard analytics and reports use the current
SQLite records.

## Rules (edit in db.py)
LOAN_DAYS = 14, FINE_PER_DAY = 2, MAX_BOOKS = 3

## Files
app.py (routes + cover generator), models.py (relational schema),
db.py (migrations + database operations), seed_data.py (legacy seed definitions),
templates/, static/style.css

## Real cover images (optional)
Covers are generated automatically. To use real photos, put images in static/covers/
and set cover_url, e.g.:
    UPDATE books SET cover_url='/static/covers/1.jpg' WHERE book_id=1;
