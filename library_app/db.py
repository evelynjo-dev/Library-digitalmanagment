import os
import random
import sqlite3
from datetime import date, timedelta

from seed_data import BOOKS, CATEGORIES, MEMBERS

DB_FILE = os.environ.get(
    "LIBRARY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "library.db"))

LOAN_DAYS = 14       # how long a book can be kept
FINE_PER_DAY = 2     # rupees per late day
MAX_BOOKS = 3        # max books per member at a time


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ------------------------------------------------------------------ setup
def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS books (
        book_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        title     TEXT NOT NULL,
        author    TEXT NOT NULL,
        category  TEXT NOT NULL,
        rack      TEXT NOT NULL,
        shelf_row INTEGER NOT NULL,
        cover_url TEXT
    );
    CREATE TABLE IF NOT EXISTS copies (
        copy_id TEXT PRIMARY KEY,
        book_id INTEGER NOT NULL REFERENCES books(book_id),
        status  TEXT NOT NULL DEFAULT 'AVAILABLE'
    );
    CREATE TABLE IF NOT EXISTS members (
        member_id TEXT PRIMARY KEY,
        name      TEXT NOT NULL,
        phone     TEXT
    );
    CREATE TABLE IF NOT EXISTS transactions (
        txn_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        copy_id     TEXT NOT NULL REFERENCES copies(copy_id),
        member_id   TEXT NOT NULL REFERENCES members(member_id),
        issue_date  TEXT NOT NULL,
        due_date    TEXT NOT NULL,
        return_date TEXT,
        fine        INTEGER DEFAULT 0
    );
    """)
    if conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0:
        seed(conn)
    conn.commit()
    conn.close()


def seed(conn):
    """Insert 100 books, 2-6 copies each, sample members and some issued books."""
    rng = random.Random(42)
    per_category = {}
    for title, author, category in BOOKS:
        idx = per_category.get(category, 0)
        per_category[category] = idx + 1
        rack = CATEGORIES[category][0]
        shelf_row = idx // 4 + 1                      # 4 books per row, rows 1-3
        cur = conn.execute(
            "INSERT INTO books (title, author, category, rack, shelf_row) VALUES (?,?,?,?,?)",
            (title, author, category, rack, shelf_row))
        book_id = cur.lastrowid
        for n in range(1, rng.randint(2, 6) + 1):
            conn.execute("INSERT INTO copies (copy_id, book_id) VALUES (?, ?)",
                         (f"BK{book_id:03d}-C{n}", book_id))
    for m in MEMBERS:
        conn.execute("INSERT INTO members VALUES (?,?,?)", m)

    # a few books already issued so the dashboard looks alive
    copies = [r[0] for r in conn.execute(
        "SELECT MIN(copy_id) FROM copies GROUP BY book_id").fetchall()]
    rng.shuffle(copies)
    today = date.today()
    for i, copy_id in enumerate(copies[:18]):
        member_id = MEMBERS[i % len(MEMBERS)][0]
        days_ago = rng.randint(20, 30) if i < 6 else rng.randint(1, 10)   # first 6 overdue
        issued = today - timedelta(days=days_ago)
        conn.execute(
            "INSERT INTO transactions (copy_id, member_id, issue_date, due_date) VALUES (?,?,?,?)",
            (copy_id, member_id, issued.isoformat(), (issued + timedelta(days=LOAN_DAYS)).isoformat()))
        conn.execute("UPDATE copies SET status='ISSUED' WHERE copy_id=?", (copy_id,))


# ------------------------------------------------------------------ catalog queries
def catalog(conn, category=None, q=None):
    sql = """
        SELECT b.*, COUNT(c.copy_id) AS total,
               COALESCE(SUM(c.status = 'AVAILABLE'), 0) AS available
        FROM books b LEFT JOIN copies c ON c.book_id = b.book_id
        WHERE 1=1"""
    args = []
    if category:
        sql += " AND b.category = ?"
        args.append(category)
    if q:
        sql += " AND (b.title LIKE ? OR b.author LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " GROUP BY b.book_id ORDER BY b.rack, b.shelf_row, b.title"
    return conn.execute(sql, args).fetchall()


def categories(conn):
    rows = conn.execute("""
        SELECT b.category AS name, b.rack, COUNT(DISTINCT b.book_id) AS titles,
               COUNT(c.copy_id) AS copies,
               COALESCE(SUM(c.status = 'AVAILABLE'), 0) AS available
        FROM books b LEFT JOIN copies c ON c.book_id = b.book_id
        GROUP BY b.category ORDER BY b.rack""").fetchall()
    return rows


def get_book(conn, book_id):
    return conn.execute("""
        SELECT b.*, COUNT(c.copy_id) AS total,
               COALESCE(SUM(c.status = 'AVAILABLE'), 0) AS available
        FROM books b LEFT JOIN copies c ON c.book_id = b.book_id
        WHERE b.book_id = ? GROUP BY b.book_id""", (book_id,)).fetchone()


def copies_of(conn, book_id):
    return conn.execute("""
        SELECT c.copy_id, c.status, m.name AS holder, t.due_date
        FROM copies c
        LEFT JOIN transactions t ON t.copy_id = c.copy_id AND t.return_date IS NULL
        LEFT JOIN members m ON m.member_id = t.member_id
        WHERE c.book_id = ? ORDER BY c.copy_id""", (book_id,)).fetchall()


# ------------------------------------------------------------------ dashboard queries
def stats(conn):
    today = date.today().isoformat()
    return {
        "titles": conn.execute("SELECT COUNT(*) FROM books").fetchone()[0],
        "copies": conn.execute("SELECT COUNT(*) FROM copies").fetchone()[0],
        "available": conn.execute("SELECT COUNT(*) FROM copies WHERE status='AVAILABLE'").fetchone()[0],
        "issued": conn.execute("SELECT COUNT(*) FROM copies WHERE status='ISSUED'").fetchone()[0],
        "members": conn.execute("SELECT COUNT(*) FROM members").fetchone()[0],
        "overdue": conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE return_date IS NULL AND due_date < ?",
            (today,)).fetchone()[0],
    }


def overdue_list(conn):
    today = date.today()
    rows = conn.execute("""
        SELECT m.name, m.member_id, b.title, t.copy_id, t.due_date
        FROM transactions t
        JOIN members m ON m.member_id = t.member_id
        JOIN copies  c ON c.copy_id = t.copy_id
        JOIN books   b ON b.book_id = c.book_id
        WHERE t.return_date IS NULL AND t.due_date < ?
        ORDER BY t.due_date""", (today.isoformat(),)).fetchall()
    out = []
    for r in rows:
        late = (today - date.fromisoformat(r["due_date"])).days
        out.append({**dict(r), "late_days": late, "fine": late * FINE_PER_DAY})
    return out


def recent_transactions(conn, limit=10):
    return conn.execute("""
        SELECT t.*, m.name AS member_name, b.title
        FROM transactions t
        JOIN members m ON m.member_id = t.member_id
        JOIN copies  c ON c.copy_id = t.copy_id
        JOIN books   b ON b.book_id = c.book_id
        ORDER BY t.txn_id DESC LIMIT ?""", (limit,)).fetchall()


def most_borrowed(conn, limit=5):
    return conn.execute("""
        SELECT b.book_id, b.title, b.author, b.rack, COUNT(t.txn_id) AS borrow_count
        FROM transactions t
        JOIN copies c ON c.copy_id = t.copy_id
        JOIN books b ON b.book_id = c.book_id
        GROUP BY b.book_id
        ORDER BY borrow_count DESC, b.title
        LIMIT ?""", (limit,)).fetchall()


def members_with_counts(conn):
    return conn.execute("""
        SELECT m.*, COUNT(t.txn_id) AS holding
        FROM members m
        LEFT JOIN transactions t ON t.member_id = m.member_id AND t.return_date IS NULL
        GROUP BY m.member_id ORDER BY m.member_id""").fetchall()


def add_member(conn, member_id, name, phone):
    member_id = member_id.strip().upper()
    if not member_id or not name.strip():
        return False, "Member ID and name are required."
    if conn.execute("SELECT 1 FROM members WHERE member_id=?", (member_id,)).fetchone():
        return False, f"Member ID {member_id} already exists."
    conn.execute("INSERT INTO members VALUES (?,?,?)", (member_id, name.strip(), phone.strip()))
    conn.commit()
    return True, f"Member {name.strip()} ({member_id}) added."


# ------------------------------------------------------------------ core: scan to issue / return
def scan_issue(conn, member_id, copy_id):
    member_id, copy_id = member_id.strip().upper(), copy_id.strip().upper()
    member = conn.execute("SELECT * FROM members WHERE member_id=?", (member_id,)).fetchone()
    if not member:
        return False, f"Member {member_id} not found."
    copy = conn.execute("""SELECT c.*, b.title FROM copies c JOIN books b ON b.book_id=c.book_id
                           WHERE c.copy_id=?""", (copy_id,)).fetchone()
    if not copy:
        return False, f"Invalid scan: copy {copy_id} does not exist."
    if copy["status"] == "ISSUED":
        return False, f"{copy['title']} ({copy_id}) is already issued."
    held = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE member_id=? AND return_date IS NULL",
        (member_id,)).fetchone()[0]
    if held >= MAX_BOOKS:
        return False, f"{member['name']} already has {MAX_BOOKS} books (limit reached)."

    today = date.today()
    due = today + timedelta(days=LOAN_DAYS)
    conn.execute(
        "INSERT INTO transactions (copy_id, member_id, issue_date, due_date) VALUES (?,?,?,?)",
        (copy_id, member_id, today.isoformat(), due.isoformat()))
    conn.execute("UPDATE copies SET status='ISSUED' WHERE copy_id=?", (copy_id,))
    conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) FROM copies WHERE book_id=? AND status='AVAILABLE'",
        (copy["book_id"],)).fetchone()[0]
    return True, (f"Issued '{copy['title']}' to {member['name']}. "
                  f"Due {due.strftime('%d %b %Y')}. Copies left: {left}.")


def scan_return(conn, copy_id):
    copy_id = copy_id.strip().upper()
    txn = conn.execute("""SELECT t.*, b.title, b.book_id FROM transactions t
                          JOIN copies c ON c.copy_id=t.copy_id JOIN books b ON b.book_id=c.book_id
                          WHERE t.copy_id=? AND t.return_date IS NULL""", (copy_id,)).fetchone()
    if not txn:
        return False, f"Copy {copy_id} is not currently issued."
    today = date.today()
    late = max(0, (today - date.fromisoformat(txn["due_date"])).days)
    fine = late * FINE_PER_DAY
    conn.execute("UPDATE transactions SET return_date=?, fine=? WHERE txn_id=?",
                 (today.isoformat(), fine, txn["txn_id"]))
    conn.execute("UPDATE copies SET status='AVAILABLE' WHERE copy_id=?", (copy_id,))
    conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) FROM copies WHERE book_id=? AND status='AVAILABLE'",
        (txn["book_id"],)).fetchone()[0]
    note = f"Returned late by {late} day(s). Fine: Rs.{fine}." if fine else "Returned on time."
    return True, f"'{txn['title']}' returned. {note} Copies available: {left}."
