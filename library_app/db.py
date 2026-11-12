import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy import and_, case, func, inspect, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import generate_password_hash

from models import (
    ActivityLog, Book, BookCopy, Category, Fine, Notification, Rack, Reservation,
    SchemaMigration, Shelf, Transaction, User, db,
)


DB_FILE = os.environ.get(
    "LIBRARY_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "library.db"))

LOAN_DAYS = 14
FINE_PER_DAY = 2
MAX_BOOKS = 3


def student_qr_payload(member_id):
    return f"SMARTLIBRARY:STUDENT:{member_id}"


def copy_qr_payload(copy_id):
    return f"SMARTLIBRARY:COPY:{copy_id}"


class ValidationError(Exception):
    pass


class NotFoundError(ValidationError):
    pass


class ConflictError(ValidationError):
    pass


class DatabaseError(Exception):
    pass


def init_app(app):
    if not app.config.get("SQLALCHEMY_DATABASE_URI"):
        uri_path = os.path.abspath(DB_FILE).replace("\\", "/")
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{uri_path}"
    app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    app.config.setdefault("SQLALCHEMY_ENGINE_OPTIONS", {"connect_args": {"timeout": 30}})
    db.init_app(app)


def _table_names():
    return set(inspect(db.engine).get_table_names())


def _rename_legacy_tables():
    raw = db.engine.raw_connection()
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.commit()
        raw.execute("BEGIN EXCLUSIVE")
        tables = {row[0] for row in raw.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table in ("books", "copies", "members", "transactions"):
            old_name = f"legacy_v1_{table}"
            if table in tables and old_name not in tables:
                raw.execute(f'ALTER TABLE "{table}" RENAME TO "{old_name}"')
        raw.commit()
        raw.execute("PRAGMA foreign_keys=ON")
        raw.commit()
    except sqlite3.Error:
        raw.rollback()
        raise
    finally:
        raw.close()


def _legacy_rows(table):
    if table not in _table_names():
        return []
    with db.engine.connect() as connection:
        return [dict(row._mapping) for row in connection.exec_driver_sql(
            f'SELECT * FROM "{table}"').fetchall()]


def _legacy_date(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DatabaseError(f"Cannot migrate invalid date value: {value!r}.") from exc


def _migrate_legacy_records():
    books = _legacy_rows("legacy_v1_books")
    copies = _legacy_rows("legacy_v1_copies")
    members = _legacy_rows("legacy_v1_members")
    transactions = _legacy_rows("legacy_v1_transactions")
    if not any((books, copies, members, transactions)):
        return

    active_copy_ids = {
        row["copy_id"] for row in transactions if row.get("return_date") is None
    }
    inconsistent = [
        row["copy_id"] for row in copies
        if (row.get("status") == "ISSUED") != (row["copy_id"] in active_copy_ids)
    ]
    if inconsistent:
        raise DatabaseError(
            "Legacy copy availability does not match active loans for: "
            + ", ".join(inconsistent[:10]))

    try:
        with db.session.begin():
            rack_codes = sorted({str(row["rack"]).strip().upper() for row in books})
            rack_by_code = {}
            for code in rack_codes:
                rack = Rack(rack_code=code, name=f"Rack {code}")
                db.session.add(rack)
                rack_by_code[code] = rack
            db.session.flush()

            category_racks = {}
            for row in books:
                category_racks.setdefault(
                    str(row["category"]).strip(), str(row["rack"]).strip().upper())
            category_by_name = {}
            try:
                from seed_data import CATEGORIES as LEGACY_CATEGORY_COLORS
            except ImportError:
                LEGACY_CATEGORY_COLORS = {}
            for name, rack_code in category_racks.items():
                colors = LEGACY_CATEGORY_COLORS.get(name, (None, "#315e53", "#2dd4bf"))
                category = Category(
                    name=name,
                    rack_id=rack_by_code[rack_code].id,
                    color_start=colors[1],
                    color_end=colors[2],
                )
                db.session.add(category)
                category_by_name[name] = category
            db.session.flush()

            shelf_by_location = {}
            for row in books:
                code = str(row["rack"]).strip().upper()
                number = int(row["shelf_row"])
                key = (code, number)
                if key not in shelf_by_location:
                    shelf = Shelf(
                        rack_id=rack_by_code[code].id,
                        shelf_number=number,
                        name=f"Shelf {number}",
                    )
                    db.session.add(shelf)
                    shelf_by_location[key] = shelf
            db.session.flush()

            user_by_member_id = {}
            for row in members:
                user = User(
                    member_id=row["member_id"],
                    qr_code=student_qr_payload(str(row["member_id"]).strip().upper()),
                    name=row["name"],
                    phone=row.get("phone"),
                    role="STUDENT",
                )
                db.session.add(user)
                user_by_member_id[user.member_id] = user
            db.session.flush()

            book_by_id = {}
            for row in books:
                category_name = str(row["category"]).strip()
                rack_code = str(row["rack"]).strip().upper()
                shelf_number = int(row["shelf_row"])
                book = Book(
                    id=row["book_id"],
                    title=row["title"],
                    author=row["author"],
                    category=category_by_name[category_name],
                    shelf=shelf_by_location[(rack_code, shelf_number)],
                    cover_url=row.get("cover_url"),
                )
                db.session.add(book)
                book_by_id[book.id] = book
            db.session.flush()

            copy_by_id = {}
            for row in copies:
                copy = BookCopy(
                    copy_id=row["copy_id"],
                    qr_code=copy_qr_payload(str(row["copy_id"]).strip().upper()),
                    book=book_by_id[row["book_id"]],
                )
                db.session.add(copy)
                copy_by_id[copy.copy_id] = copy
            db.session.flush()

            for row in transactions:
                member_id = str(row["member_id"]).upper()
                if row["copy_id"] not in copy_by_id or member_id not in user_by_member_id:
                    raise DatabaseError(
                        f"Cannot migrate transaction {row['txn_id']}: linked copy or member is missing.")
                transaction = Transaction(
                    id=row["txn_id"],
                    copy_id=row["copy_id"],
                    user=user_by_member_id[member_id],
                    issue_date=_legacy_date(row["issue_date"]),
                    due_date=_legacy_date(row["due_date"]),
                    return_date=_legacy_date(row["return_date"]) if row.get("return_date") else None,
                )
                db.session.add(transaction)
                fine_amount = int(row.get("fine") or 0)
                if fine_amount > 0:
                    db.session.add(Fine(transaction=transaction, amount=fine_amount))

            db.session.flush()
    except (IntegrityError, SQLAlchemyError) as exc:
        db.session.rollback()
        raise DatabaseError("Legacy database migration failed; the original tables were retained.") from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise DatabaseError("Legacy database contains invalid records and was not migrated.") from exc


def init_db():
    tables = _table_names()
    if "books" in tables:
        columns = {column["name"] for column in inspect(db.engine).get_columns("books")}
        if "category_id" not in columns:
            _rename_legacy_tables()

    db.create_all()
    if db.session.get(SchemaMigration, 1) is None:
        db.session.rollback()
        if "legacy_v1_books" in _table_names():
            _migrate_legacy_records()
        db.session.add(SchemaMigration(version=1))
        db.session.commit()
    if db.session.get(SchemaMigration, 2) is None:
        db.session.rollback()
        _upgrade_qr_codes()
        db.session.add(SchemaMigration(version=2))
        db.session.commit()
    if db.session.get(SchemaMigration, 3) is None:
        db.session.rollback()
        _upgrade_auth_notifications()
        db.session.add(SchemaMigration(version=3))
        db.session.commit()
    if db.session.get(SchemaMigration, 4) is None:
        db.session.rollback()
        _upgrade_user_emails()
        db.session.add(SchemaMigration(version=4))
        db.session.commit()


def _upgrade_qr_codes():
    try:
        columns = {column["name"] for column in inspect(db.engine).get_columns("users")}
        with db.engine.begin() as connection:
            if "qr_code" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN qr_code VARCHAR(200) NOT NULL DEFAULT ''")
            users = connection.exec_driver_sql(
                "SELECT id, member_id, qr_code FROM users").fetchall()
            for user_id, member_id, qr_code in users:
                if not qr_code:
                    connection.exec_driver_sql(
                        "UPDATE users SET qr_code = ? WHERE id = ?",
                        (student_qr_payload(str(member_id).strip().upper()), user_id),
                    )
            copies = connection.exec_driver_sql(
                "SELECT copy_id, qr_code FROM book_copies").fetchall()
            for copy_id, qr_code in copies:
                if not qr_code or qr_code == copy_id:
                    connection.exec_driver_sql(
                        "UPDATE book_copies SET qr_code = ? WHERE copy_id = ?",
                        (copy_qr_payload(str(copy_id).strip().upper()), copy_id),
                    )
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_qr_code ON users (qr_code)")
    except SQLAlchemyError as exc:
        raise DatabaseError(
            "The QR-code database migration failed; existing records were preserved.") from exc


def _upgrade_auth_notifications():
    try:
        with db.engine.begin() as connection:
            user_columns = {
                column["name"] for column in inspect(db.engine).get_columns("users")
            }
            if "password_hash" not in user_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)")
            notification_columns = {
                column["name"]
                for column in inspect(db.engine).get_columns("notifications")
            }
            if "transaction_id" not in notification_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE notifications ADD COLUMN transaction_id INTEGER "
                    "REFERENCES transactions(id) ON DELETE CASCADE")
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_transaction_kind "
                "ON notifications (transaction_id, kind)")
    except SQLAlchemyError as exc:
        raise DatabaseError(
            "The authentication and notification migration failed; existing records were preserved.") from exc


def _upgrade_user_emails():
    try:
        columns = {column["name"] for column in inspect(db.engine).get_columns("users")}
        with db.engine.begin() as connection:
            if "email" not in columns:
                connection.exec_driver_sql("ALTER TABLE users ADD COLUMN email VARCHAR(254)")
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_nocase "
                "ON users (email COLLATE NOCASE) WHERE email IS NOT NULL")
    except SQLAlchemyError as exc:
        raise DatabaseError(
            "The account-email migration failed; existing records were preserved.") from exc


def _commit():
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ConflictError("A duplicate identifier or related record prevents this operation.") from exc
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise DatabaseError("The database operation could not be completed.") from exc


def _flush():
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise ConflictError("A duplicate identifier or invalid relationship prevents this operation.") from exc
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise DatabaseError("The database operation could not be completed.") from exc


def _required(data, key, label, max_length):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required.")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{label} must be {max_length} characters or fewer.")
    return value


def _optional(data, key, label, max_length):
    value = data.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text.")
    value = value.strip()
    if len(value) > max_length:
        raise ValidationError(f"{label} must be {max_length} characters or fewer.")
    return value or None


def _identifier(value, label):
    if isinstance(value, bool):
        raise ValidationError(f"{label} must be a positive integer.")
    try:
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
        else:
            raise ValueError
    except ValueError as exc:
        raise ValidationError(f"{label} must be a positive integer.") from exc
    if parsed < 1:
        raise ValidationError(f"{label} must be a positive integer.")
    return parsed


def _color(data, key, default):
    value = data.get(key, default)
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValidationError(f"{key.replace('_', ' ').capitalize()} must be a six-digit hex color.")
    return value


def _get_or_404(model, identifier, label):
    item = db.session.get(model, _identifier(identifier, label))
    if item is None:
        raise NotFoundError(f"{label} was not found.")
    return item


def _audit(action, entity_type, entity_id, details=None):
    db.session.add(ActivityLog(
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=json.dumps(details, ensure_ascii=False) if details is not None else None,
    ))


def _available_expr():
    return func.sum(case((Transaction.id.is_(None), 1), else_=0))


def _book_dict(book, total=0, available=0):
    rack = book.shelf.rack
    category = book.category
    return {
        "book_id": book.id,
        "id": book.id,
        "title": book.title,
        "author": book.author,
        "category": category.name,
        "category_id": category.id,
        "color_start": category.color_start,
        "color_end": category.color_end,
        "rack": rack.rack_code,
        "rack_id": rack.id,
        "shelf_row": book.shelf.shelf_number,
        "shelf_id": book.shelf.id,
        "cover_url": book.cover_url,
        "total": int(total or 0),
        "available": int(available or 0),
    }


def catalog(_session=None, category=None, q=None):
    query = db.session.query(
        Book,
        func.count(BookCopy.id),
        _available_expr(),
    ).join(Category, Book.category_id == Category.id).join(
        Shelf, Book.shelf_id == Shelf.id).join(
        Rack, Shelf.rack_id == Rack.id).outerjoin(BookCopy).outerjoin(
        Transaction,
        and_(Transaction.copy_id == BookCopy.copy_id, Transaction.return_date.is_(None)),
    )
    if category:
        query = query.filter(Category.name == category)
    if q:
        needle = f"%{q}%"
        query = query.filter(or_(Book.title.ilike(needle), Book.author.ilike(needle)))
    rows = query.group_by(Book.id).order_by(
        Rack.rack_code, Shelf.shelf_number, Book.title).all()
    return [_book_dict(book, total, available) for book, total, available in rows]


def categories(_session=None):
    query = db.session.query(
        Category,
        Rack.rack_code,
        func.count(func.distinct(Book.id)),
        func.count(func.distinct(BookCopy.id)),
        func.count(func.distinct(case((Transaction.id.is_(None), BookCopy.id), else_=None))),
    ).outerjoin(Rack, Category.rack_id == Rack.id).outerjoin(
        Book, Book.category_id == Category.id).outerjoin(
        Shelf, Book.shelf_id == Shelf.id).outerjoin(
        BookCopy, BookCopy.book_id == Book.id).outerjoin(
        Transaction,
        and_(Transaction.copy_id == BookCopy.copy_id, Transaction.return_date.is_(None)),
    ).group_by(Category.id, Rack.rack_code).order_by(Rack.rack_code, Category.name)
    return [
        {
            "id": category.id,
            "name": category.name,
            "rack": rack_code or "",
            "rack_id": category.rack_id,
            "description": category.description,
            "color_start": category.color_start,
            "color_end": category.color_end,
            "titles": int(titles or 0),
            "copies": int(copies or 0),
            "available": int(available or 0),
        }
        for category, rack_code, titles, copies, available in query.all()
    ]


def get_book(_session, book_id):
    book = db.session.get(Book, _identifier(book_id, "Book ID"))
    if book is None:
        return None
    total = db.session.query(func.count(BookCopy.id)).filter_by(book_id=book.id).scalar()
    available = db.session.query(func.count(BookCopy.id)).outerjoin(
        Transaction,
        and_(Transaction.copy_id == BookCopy.copy_id, Transaction.return_date.is_(None)),
    ).filter(BookCopy.book_id == book.id, Transaction.id.is_(None)).scalar()
    return _book_dict(book, total, available)


def copies_of(_session, book_id):
    identifier = _identifier(book_id, "Book ID")
    rows = db.session.query(BookCopy, Transaction, User).outerjoin(
        Transaction,
        and_(Transaction.copy_id == BookCopy.copy_id, Transaction.return_date.is_(None)),
    ).outerjoin(User, User.id == Transaction.user_id).filter(
        BookCopy.book_id == identifier).order_by(BookCopy.copy_id).all()
    return [
        {
            "copy_id": copy.copy_id,
            "qr_code": copy.qr_code,
            "status": "ISSUED" if transaction else "AVAILABLE",
            "holder": user.name if user else None,
            "due_date": transaction.due_date.isoformat() if transaction else None,
        }
        for copy, transaction, user in rows
    ]


def copy_qr_value(copy_id):
    copy = db.session.query(BookCopy).filter(
        or_(BookCopy.copy_id == copy_id.upper(), BookCopy.qr_code == copy_id)).first()
    if copy is None:
        raise NotFoundError(f"Copy {copy_id} was not found.")
    return copy.qr_code


def labels(_session=None, book_id=None):
    query = db.session.query(BookCopy, Book.title).join(
        Book, BookCopy.book_id == Book.id)
    if book_id is not None:
        query = query.filter(Book.id == _identifier(book_id, "Book ID"))
    return [
        {"copy_id": copy.copy_id, "qr_code": copy.qr_code, "title": title}
        for copy, title in query.order_by(BookCopy.copy_id).all()
    ]


def copy_details(copy_id):
    copy = db.session.query(BookCopy).filter_by(copy_id=copy_id.upper()).first()
    if copy is None:
        raise NotFoundError("Book copy was not found.")
    return {
        "id": copy.id,
        "copy_id": copy.copy_id,
        "qr_code": copy.qr_code,
        "book_id": copy.book_id,
        "title": copy.book.title,
        "status": copy.status,
    }


def stats(_session=None):
    titles = db.session.query(func.count(Book.id)).scalar()
    copies = db.session.query(func.count(BookCopy.id)).scalar()
    issued = db.session.query(func.count(Transaction.id)).filter(
        Transaction.return_date.is_(None)).scalar()
    overdue = db.session.query(func.count(Transaction.id)).filter(
        Transaction.return_date.is_(None), Transaction.due_date < date.today()).scalar()
    members = db.session.query(func.count(User.id)).filter(User.role == "STUDENT").scalar()
    return {
        "titles": int(titles or 0),
        "copies": int(copies or 0),
        "available": int((copies or 0) - (issued or 0)),
        "issued": int(issued or 0),
        "members": int(members or 0),
        "overdue": int(overdue or 0),
    }


def analytics_data(_session=None):
    today = date.today()
    month_starts = []
    year, month = today.year, today.month
    for _ in range(12):
        month_starts.append(date(year, month, 1))
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
    month_starts.reverse()
    first_month = month_starts[0]
    if today.month == 12:
        after_last_month = date(today.year + 1, 1, 1)
    else:
        after_last_month = date(today.year, today.month + 1, 1)

    issue_rows = db.session.query(
        func.strftime("%Y-%m", Transaction.issue_date), func.count(Transaction.id)
    ).filter(
        Transaction.issue_date >= first_month,
        Transaction.issue_date < after_last_month,
    ).group_by(func.strftime("%Y-%m", Transaction.issue_date)).all()
    return_rows = db.session.query(
        func.strftime("%Y-%m", Transaction.return_date), func.count(Transaction.id)
    ).filter(
        Transaction.return_date.is_not(None),
        Transaction.return_date >= first_month,
        Transaction.return_date < after_last_month,
    ).group_by(func.strftime("%Y-%m", Transaction.return_date)).all()
    issues_by_month = {key: count for key, count in issue_rows}
    returns_by_month = {key: count for key, count in return_rows}

    active_students = db.session.query(
        User.member_id, User.name, func.count(Transaction.id)
    ).join(Transaction, Transaction.user_id == User.id).filter(
        User.role == "STUDENT"
    ).group_by(User.id).order_by(
        func.count(Transaction.id).desc(), User.name
    ).limit(5).all()
    category_borrowing = db.session.query(
        Category.name, func.count(Transaction.id)
    ).join(Book, Book.category_id == Category.id).join(
        BookCopy, BookCopy.book_id == Book.id
    ).join(Transaction, Transaction.copy_id == BookCopy.copy_id).group_by(
        Category.id
    ).order_by(func.count(Transaction.id).desc(), Category.name).all()
    overdue = overdue_list()
    inventory = stats()
    return {
        "months": [month.strftime("%Y-%m") for month in month_starts],
        "monthly_issues": [
            int(issues_by_month.get(month.strftime("%Y-%m"), 0))
            for month in month_starts
        ],
        "monthly_returns": [
            int(returns_by_month.get(month.strftime("%Y-%m"), 0))
            for month in month_starts
        ],
        "most_borrowed": most_borrowed(5),
        "active_students": [
            {"member_id": member_id, "name": name, "borrow_count": int(count)}
            for member_id, name, count in active_students
        ],
        "category_borrowing": [
            {"name": name, "borrow_count": int(count)}
            for name, count in category_borrowing
        ],
        "overdue": {
            "count": inventory["overdue"],
            "estimated_fines": sum(row["fine"] for row in overdue),
        },
        "inventory": inventory,
    }


def overdue_list(_session=None):
    today = date.today()
    rows = db.session.query(Transaction, User, BookCopy, Book).join(
        User, Transaction.user_id == User.id).join(
        BookCopy, Transaction.copy_id == BookCopy.copy_id).join(
        Book, BookCopy.book_id == Book.id).filter(
        Transaction.return_date.is_(None), Transaction.due_date < today
    ).order_by(Transaction.due_date).all()
    return [
        {
            "name": user.name,
            "member_id": user.member_id,
            "title": book.title,
            "copy_id": copy.copy_id,
            "due_date": transaction.due_date.isoformat(),
            "late_days": (today - transaction.due_date).days,
            "fine": (today - transaction.due_date).days * FINE_PER_DAY,
        }
        for transaction, user, copy, book in rows
    ]


def member_borrowing_report(member_id):
    member = db.session.query(User).filter_by(
        member_id=_required(
            {"member_id": member_id}, "member_id", "Member ID", 64).upper(),
        role="STUDENT",
    ).first()
    if member is None:
        return None
    rows = db.session.query(Transaction, BookCopy, Book, Fine).join(
        BookCopy, Transaction.copy_id == BookCopy.copy_id
    ).join(Book, BookCopy.book_id == Book.id).outerjoin(
        Fine, Fine.transaction_id == Transaction.id
    ).filter(
        Transaction.user_id == member.id
    ).order_by(Transaction.issue_date.desc(), Transaction.id.desc()).all()
    today = date.today()
    history = []
    for transaction, copy, book, fine in rows:
        history.append({
            "title": book.title,
            "copy_id": copy.copy_id,
            "issue_date": transaction.issue_date.isoformat(),
            "due_date": transaction.due_date.isoformat(),
            "return_date": transaction.return_date.isoformat()
            if transaction.return_date else "",
            "status": "Returned" if transaction.return_date else "On loan",
            "fine": fine.amount if fine else (
                max(0, (today - transaction.due_date).days) * FINE_PER_DAY
                if transaction.return_date is None else 0
            ),
        })
    return {"member_id": member.member_id, "name": member.name, "history": history}


def inventory_report():
    rows = db.session.query(
        BookCopy, Book, Category, Rack, Shelf, Transaction
    ).join(Book, BookCopy.book_id == Book.id).join(
        Category, Book.category_id == Category.id
    ).join(Shelf, Book.shelf_id == Shelf.id).join(
        Rack, Shelf.rack_id == Rack.id
    ).outerjoin(
        Transaction,
        and_(
            Transaction.copy_id == BookCopy.copy_id,
            Transaction.return_date.is_(None),
        ),
    ).order_by(Book.title, BookCopy.copy_id).all()
    return [
        {
            "copy_id": copy.copy_id,
            "qr_code": copy.qr_code,
            "title": book.title,
            "author": book.author,
            "category": category.name,
            "rack": rack.rack_code,
            "shelf": shelf.shelf_number,
            "status": "Issued" if transaction else "Available",
        }
        for copy, book, category, rack, shelf, transaction in rows
    ]


def recent_transactions(_session=None, limit=10):
    rows = db.session.query(Transaction, User, BookCopy, Book, Fine).join(
        User, Transaction.user_id == User.id).join(
        BookCopy, Transaction.copy_id == BookCopy.copy_id).join(
        Book, BookCopy.book_id == Book.id).outerjoin(
        Fine, Fine.transaction_id == Transaction.id
    ).order_by(Transaction.id.desc()).limit(_identifier(limit, "Limit")).all()
    return [
        {
            "txn_id": transaction.id,
            "copy_id": copy.copy_id,
            "member_id": user.member_id,
            "member_name": user.name,
            "title": book.title,
            "issue_date": transaction.issue_date.isoformat(),
            "due_date": transaction.due_date.isoformat(),
            "return_date": transaction.return_date.isoformat() if transaction.return_date else None,
            "fine": fine.amount if fine else 0,
        }
        for transaction, user, copy, book, fine in rows
    ]


def most_borrowed(_session=None, limit=5):
    rows = db.session.query(
        Book, Rack.rack_code, func.count(Transaction.id)
    ).join(BookCopy, BookCopy.book_id == Book.id).join(
        Transaction, Transaction.copy_id == BookCopy.copy_id
    ).join(Shelf, Book.shelf_id == Shelf.id).join(
        Rack, Shelf.rack_id == Rack.id
    ).group_by(Book.id, Rack.rack_code).order_by(
        func.count(Transaction.id).desc(), Book.title
    ).limit(_identifier(limit, "Limit")).all()
    return [
        {
            "book_id": book.id,
            "title": book.title,
            "author": book.author,
            "rack": rack_code,
            "borrow_count": count,
        }
        for book, rack_code, count in rows
    ]


def members_with_counts(_session=None):
    rows = db.session.query(User, func.count(Transaction.id)).outerjoin(
        Transaction,
        and_(Transaction.user_id == User.id, Transaction.return_date.is_(None)),
    ).filter(User.role == "STUDENT").group_by(User.id).order_by(User.member_id).all()
    return [
        {
            "id": user.id,
            "member_id": user.member_id,
            "qr_code": user.qr_code,
            "name": user.name,
            "phone": user.phone,
            "role": user.role,
            "holding": int(holding or 0),
        }
        for user, holding in rows
    ]


def _member_by_code(member_id):
    return db.session.query(User).filter(
        User.member_id == member_id.upper(), User.role == "STUDENT").first()


def _member_by_scan(code):
    return db.session.query(User).filter(
        or_(User.member_id == code.upper(), User.qr_code == code),
        User.role == "STUDENT",
    ).first()


def _copy_by_scan(code):
    return db.session.query(BookCopy).filter(
        or_(BookCopy.copy_id == code.upper(), BookCopy.qr_code == code)
    ).first()


def _student_scan_details(user):
    held = db.session.query(func.count(Transaction.id)).filter(
        Transaction.user_id == user.id,
        Transaction.return_date.is_(None),
    ).scalar()
    return {
        "member_id": user.member_id,
        "qr_code": user.qr_code,
        "name": user.name,
        "phone": user.phone,
        "books_held": int(held or 0),
        "borrowing_limit": MAX_BOOKS,
        "eligible": held < MAX_BOOKS,
    }


def _copy_scan_details(copy):
    active = db.session.query(Transaction).filter_by(
        copy_id=copy.copy_id, return_date=None).first()
    shelf = copy.book.shelf
    return {
        "copy_id": copy.copy_id,
        "qr_code": copy.qr_code,
        "book_id": copy.book_id,
        "title": copy.book.title,
        "author": copy.book.author,
        "category": copy.book.category.name,
        "rack": shelf.rack.rack_code,
        "shelf": shelf.shelf_number,
        "available": active is None,
    }


def lookup_scan(code, mode, member_id=None):
    code = _required({"value": code}, "value", "QR code or ID", 200)
    mode = _required({"value": mode}, "value", "Scan mode", 16).lower()
    if mode not in {"issue", "return"}:
        raise ValidationError("Scan mode must be issue or return.")

    if mode == "issue" and not member_id:
        user = _member_by_scan(code)
        if user is None:
            raise NotFoundError("Student library card or ID was not recognized.")
        student = _student_scan_details(user)
        return {
            "kind": "student",
            "student": student,
            "message": (
                "Student is ready to borrow a book."
                if student["eligible"]
                else f"{user.name} has reached the {MAX_BOOKS}-book borrowing limit."
            ),
        }

    if mode == "issue":
        student_code = _required(
            {"value": member_id}, "value", "Student ID or library card", 200)
        user = _member_by_scan(student_code)
        if user is None:
            raise NotFoundError("Student library card or ID was not recognized.")
        student = _student_scan_details(user)
        if not student["eligible"]:
            raise ConflictError(
                f"{user.name} already has {MAX_BOOKS} books (limit reached).")
        copy = _copy_by_scan(code)
        if copy is None:
            raise NotFoundError("Book QR code or copy ID was not recognized.")
        if db.session.query(Transaction.id).filter_by(
                copy_id=copy.copy_id, return_date=None).first():
            raise ConflictError(f"{copy.book.title} ({copy.copy_id}) is already issued.")
        return {
            "kind": "copy",
            "student": student,
            "copy": _copy_scan_details(copy),
            "message": "Book copy is available to issue.",
        }

    copy = _copy_by_scan(code)
    if copy is None:
        raise NotFoundError("Book QR code or copy ID was not recognized.")
    transaction = db.session.query(Transaction).filter_by(
        copy_id=copy.copy_id, return_date=None).first()
    if transaction is None:
        raise ConflictError(f"Copy {copy.copy_id} is not currently issued.")
    return {
        "kind": "copy",
        "student": _student_scan_details(transaction.user),
        "copy": _copy_scan_details(copy),
        "transaction": {
            "txn_id": transaction.id,
            "issue_date": transaction.issue_date.isoformat(),
            "due_date": transaction.due_date.isoformat(),
        },
        "message": "Active loan found. Ready to process the return.",
    }


def member_card(member_id):
    user = _member_by_code(member_id)
    if user is None:
        raise NotFoundError("Member was not found.")
    details = _student_scan_details(user)
    details["role"] = user.role
    return details


def _reservation_dict(reservation, position=None):
    return {
        "id": reservation.id,
        "book_id": reservation.book_id,
        "title": reservation.book.title,
        "author": reservation.book.author,
        "status": reservation.status,
        "reserved_at": reservation.reserved_at.isoformat(),
        "fulfilled_at": reservation.fulfilled_at.isoformat() if reservation.fulfilled_at else None,
        "queue_position": position,
    }


def _reservation_position(reservation):
    if reservation.status not in {"PENDING", "READY"}:
        return None
    active = db.session.query(Reservation.id).filter(
        Reservation.book_id == reservation.book_id,
        Reservation.status.in_(("PENDING", "READY")),
        or_(
            Reservation.reserved_at < reservation.reserved_at,
            and_(
                Reservation.reserved_at == reservation.reserved_at,
                Reservation.id <= reservation.id,
            ),
        ),
    ).order_by(Reservation.reserved_at, Reservation.id).all()
    return len(active)


def _active_holds(user_id):
    return db.session.query(func.count(Transaction.id)).filter(
        Transaction.user_id == user_id,
        Transaction.return_date.is_(None),
    ).scalar() or 0


def _available_copy_count(book_id):
    return db.session.query(BookCopy.id).outerjoin(
        Transaction,
        and_(Transaction.copy_id == BookCopy.copy_id, Transaction.return_date.is_(None)),
    ).filter(BookCopy.book_id == book_id, Transaction.id.is_(None)).count()


def _advance_reservation_queue(book_id):
    ready = db.session.query(Reservation).filter_by(
        book_id=book_id, status="READY").order_by(
            Reservation.reserved_at, Reservation.id).all()
    for reservation in ready:
        if _active_holds(reservation.user_id) >= MAX_BOOKS:
            reservation.status = "PENDING"

    ready_count = db.session.query(func.count(Reservation.id)).filter_by(
        book_id=book_id, status="READY").scalar() or 0
    slots = max(0, _available_copy_count(book_id) - ready_count)
    if not slots:
        return

    pending = db.session.query(Reservation).filter_by(
        book_id=book_id, status="PENDING").order_by(
            Reservation.reserved_at, Reservation.id).all()
    for reservation in pending:
        user = reservation.user
        if user.role != "STUDENT" or _active_holds(user.id) >= MAX_BOOKS:
            continue
        reservation.status = "READY"
        message = f"Reservation #{reservation.id} for '{reservation.book.title}' is ready."
        existing_notice = db.session.query(Notification.id).filter_by(
            user_id=user.id, kind="RESERVATION_READY", message=message).first()
        if existing_notice is None:
            db.session.add(Notification(
                user_id=user.id, kind="RESERVATION_READY", message=message))
        _audit("RESERVATION_READY", "Reservation", reservation.id, {
            "book_id": reservation.book_id, "member_id": user.member_id})
        slots -= 1
        if not slots:
            break


def _refresh_reservations_for_student(user_id):
    book_ids = db.session.query(Reservation.book_id).filter(
        Reservation.user_id == user_id,
        Reservation.status.in_(("PENDING", "READY")),
    ).distinct().order_by(Reservation.book_id).all()
    for (book_id,) in book_ids:
        _advance_reservation_queue(book_id)


def create_reservation(member_id, book_id):
    user = _member_by_code(member_id)
    if user is None:
        raise NotFoundError("Student was not found.")
    book = _get_or_404(Book, book_id, "Book")
    duplicate = db.session.query(Reservation.id).filter(
        Reservation.user_id == user.id,
        Reservation.book_id == book.id,
        Reservation.status.in_(("PENDING", "READY")),
    ).first()
    if duplicate:
        raise ConflictError("You already have an active reservation for this book.")
    reservation = Reservation(user_id=user.id, book_id=book.id, status="PENDING")
    db.session.add(reservation)
    _flush()
    _audit("RESERVE", "Reservation", reservation.id, {
        "book_id": book.id, "member_id": user.member_id})
    _advance_reservation_queue(book.id)
    _commit()
    return _reservation_dict(reservation, _reservation_position(reservation))


def cancel_reservation(reservation_id, member_id):
    user = _member_by_code(member_id)
    reservation = db.session.get(Reservation, _identifier(reservation_id, "Reservation ID"))
    if reservation is None or user is None or reservation.user_id != user.id:
        raise NotFoundError("Reservation was not found.")
    if reservation.status not in {"PENDING", "READY"}:
        raise ConflictError("Only active reservations can be cancelled.")
    book_id = reservation.book_id
    reservation.status = "CANCELLED"
    reservation.fulfilled_at = datetime.now()
    _audit("CANCEL_RESERVATION", "Reservation", reservation.id, {"book_id": book_id})
    _advance_reservation_queue(book_id)
    _commit()
    return _reservation_dict(reservation)


def student_portal_data(member_id):
    user = _member_by_code(member_id)
    if user is None:
        raise NotFoundError("Student was not found.")
    transactions = db.session.query(Transaction).filter_by(
        user_id=user.id).order_by(Transaction.issue_date.desc(), Transaction.id.desc()).all()
    history = []
    active_loans = []
    for transaction in transactions:
        copy = transaction.copy
        book = copy.book
        row = {
            "transaction_id": transaction.id,
            "book_id": book.id,
            "copy_id": copy.copy_id,
            "title": book.title,
            "author": book.author,
            "category": book.category.name,
            "rack": book.shelf.rack.rack_code,
            "shelf": book.shelf.shelf_number,
            "issue_date": transaction.issue_date.isoformat(),
            "due_date": transaction.due_date.isoformat(),
            "return_date": transaction.return_date.isoformat() if transaction.return_date else None,
            "fine": transaction.fine_record.amount if transaction.fine_record else 0,
            "overdue_days": max(0, (date.today() - transaction.due_date).days)
            if transaction.return_date is None else 0,
        }
        history.append(row)
        if transaction.return_date is None:
            active_loans.append(row)
    fines = db.session.query(Fine).join(
        Transaction, Fine.transaction_id == Transaction.id
    ).filter(
        Transaction.user_id == user.id,
        Fine.amount > Fine.paid_amount,
    ).order_by(Fine.assessed_at.desc()).all()
    reservations = db.session.query(Reservation).filter_by(
        user_id=user.id).order_by(Reservation.reserved_at.desc(), Reservation.id.desc()).all()
    notifications = db.session.query(Notification).filter_by(
        user_id=user.id).order_by(Notification.created_at.desc(), Notification.id.desc()).limit(50).all()
    return {
        "profile": member_card(user.member_id),
        "active_loans": active_loans,
        "history": history,
        "fines": [
            {
                "transaction_id": fine.transaction_id,
                "title": fine.transaction.copy.book.title,
                "copy_id": fine.transaction.copy_id,
                "amount": fine.amount,
                "paid_amount": fine.paid_amount,
                "balance": fine.amount - fine.paid_amount,
                "assessed_at": fine.assessed_at.isoformat(),
            }
            for fine in fines
        ],
        "fine_total": sum(fine.amount - fine.paid_amount for fine in fines),
        "reservations": [
            _reservation_dict(reservation, _reservation_position(reservation))
            for reservation in reservations
        ],
        "notifications": [
            {
                "id": notification.id,
                "kind": notification.kind,
                "message": notification.message,
                "created_at": notification.created_at.isoformat(),
                "read": notification.read_at is not None,
            }
            for notification in notifications
        ],
    }


def mark_notification_read(notification_id, member_id):
    user = _member_by_code(member_id)
    notification = db.session.get(
        Notification, _identifier(notification_id, "Notification ID"))
    if notification is None or user is None or notification.user_id != user.id:
        raise NotFoundError("Notification was not found.")
    if notification.read_at is None:
        notification.read_at = datetime.now()
        _commit()
    return notification.id


def set_member_password(member_id, password):
    user = _member_by_code(member_id)
    if user is None:
        raise NotFoundError("Member was not found.")
    if not isinstance(password, str) or len(password) < 12 or len(password) > 128:
        raise ValidationError("Password must be between 12 and 128 characters.")
    user.password_hash = generate_password_hash(password)
    _audit("SET_PASSWORD", "User", user.id, {"member_id": user.member_id})
    _commit()


def bootstrap_admin(member_id, name, password):
    member_id = _required({"value": member_id}, "value", "Administrator ID", 64).upper()
    name = _required({"value": name}, "value", "Administrator name", 160)
    if not isinstance(password, str) or len(password) < 12 or len(password) > 128:
        raise ValidationError("Administrator password must be between 12 and 128 characters.")
    user = db.session.query(User).filter_by(member_id=member_id).first()
    if user is not None:
        if user.role != "ADMIN":
            raise ConflictError("The configured administrator ID belongs to a non-administrator account.")
        if user.password_hash:
            return
        user.name = name
    else:
        user = User(
            member_id=member_id,
            qr_code=f"SMARTLIBRARY:STAFF:{member_id}",
            name=name,
            role="ADMIN",
        )
        db.session.add(user)
    user.password_hash = generate_password_hash(password)
    _audit("BOOTSTRAP_ADMIN", "User", member_id)
    _commit()


def create_student_account(name, email, password):
    name = _required({"name": name}, "name", "Name", 160)
    email = _required({"email": email}, "email", "Email", 254).lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValidationError("Enter a valid email address.")
    if not isinstance(password, str) or len(password) < 12 or len(password) > 128:
        raise ValidationError("Password must be between 12 and 128 characters.")
    if db.session.query(User).filter(func.lower(User.email) == email).first():
        raise ConflictError("An account already uses this email address.")

    member_id = f"WEB-{uuid.uuid4().hex.upper()}"
    user = User(
        member_id=member_id,
        qr_code=student_qr_payload(member_id),
        name=name,
        email=email,
        password_hash=generate_password_hash(password),
        role="STUDENT",
    )
    db.session.add(user)
    _flush()
    _audit("SELF_REGISTER", "User", user.id, {"member_id": member_id})
    _commit()
    return user


def generate_due_notifications(member_id=None):
    today = date.today()
    reminder_cutoff = today + timedelta(days=3)
    query = db.session.query(Transaction).filter(
        Transaction.return_date.is_(None),
        Transaction.due_date <= reminder_cutoff,
    )
    if member_id is not None:
        user = _member_by_code(member_id)
        if user is None:
            raise NotFoundError("Student was not found.")
        query = query.filter(Transaction.user_id == user.id)
    created = 0
    for transaction in query.order_by(Transaction.due_date, Transaction.id).all():
        kind = "OVERDUE" if transaction.due_date < today else "DUE_SOON"
        if db.session.query(Notification.id).filter_by(
                transaction_id=transaction.id, kind=kind).first():
            continue
        days = (transaction.due_date - today).days
        if kind == "OVERDUE":
            message = (
                f"'{transaction.copy.book.title}' is overdue by {-days} day(s). "
                f"Current fine: Rs.{-days * FINE_PER_DAY}.")
        else:
            message = f"'{transaction.copy.book.title}' is due in {days} day(s)."
        db.session.add(Notification(
            user_id=transaction.user_id,
            transaction_id=transaction.id,
            kind=kind,
            message=message,
        ))
        created += 1
    if created:
        _commit()
    return created


def floor_map_data(search=None):
    racks = db.session.query(Rack).order_by(Rack.rack_code).all()
    by_rack = {}
    by_shelf = {}
    for rack in racks:
        rack_data = {
            "id": rack.id,
            "rack_code": rack.rack_code,
            "name": rack.name,
            "location": rack.location,
            "shelves": [],
            "copies": 0,
            "available": 0,
            "titles": 0,
            "matches": False,
        }
        by_rack[rack.id] = rack_data
        for shelf in sorted(rack.shelves, key=lambda row: row.shelf_number):
            shelf_data = {
                "id": shelf.id,
                "number": shelf.shelf_number,
                "name": shelf.name,
                "books": [],
                "copies": 0,
                "available": 0,
                "titles": 0,
            }
            rack_data["shelves"].append(shelf_data)
            by_shelf[shelf.id] = shelf_data

    books = db.session.query(Book).order_by(Book.title).all()
    book_data = {}
    for book in books:
        shelf_data = by_shelf[book.shelf_id]
        rack_id = book.shelf.rack_id
        shelf_data["titles"] += 1
        by_rack[rack_id]["titles"] += 1
        data = {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "category": book.category.name,
            "rack": book.shelf.rack.rack_code,
            "rack_id": rack_id,
            "shelf": book.shelf.shelf_number,
            "copies": [],
            "available": 0,
            "total": 0,
            "matches": False,
        }
        shelf_data["books"].append(data)
        book_data[book.id] = data
        by_rack[rack_id]["matches"] = False

    for copy, active in db.session.query(BookCopy, Transaction).outerjoin(
        Transaction,
        and_(Transaction.copy_id == BookCopy.copy_id, Transaction.return_date.is_(None)),
    ).order_by(BookCopy.copy_id).all():
        book = book_data[copy.book_id]
        available = active is None
        book["copies"].append({
            "copy_id": copy.copy_id,
            "status": "AVAILABLE" if available else "ISSUED",
            "due_date": active.due_date.isoformat() if active else None,
        })
        book["total"] += 1
        book["available"] += int(available)
        shelf = by_shelf[copy.book.shelf_id]
        shelf["copies"] += 1
        shelf["available"] += int(available)
        rack = by_rack[copy.book.shelf.rack_id]
        rack["copies"] += 1
        rack["available"] += int(available)

    term = (search or "").strip().casefold()
    for book_id, book in book_data.items():
        haystack = " ".join(
            [book["title"], book["author"], book["category"]]
            + [copy["copy_id"] for copy in book["copies"]]
        ).casefold()
        book["matches"] = bool(term and term in haystack)
        if book["matches"]:
            by_rack[book["rack_id"]]["matches"] = True

    for book in book_data.values():
        book.pop("rack_id", None)
    return list(by_rack.values())


def add_member(_session, member_id, name, phone):
    try:
        member_id = _required({"value": member_id}, "value", "Member ID", 64).upper()
        name = _required({"value": name}, "value", "Name", 160)
        phone = _optional({"value": phone}, "value", "Phone", 40)
        if _member_by_code(member_id):
            return False, f"Member ID {member_id} already exists."
        user = User(
            member_id=member_id,
            qr_code=student_qr_payload(member_id),
            name=name,
            phone=phone,
            role="STUDENT",
        )
        db.session.add(user)
        _audit("CREATE", "User", member_id, {"role": "STUDENT"})
        _commit()
        return True, f"Member {name} ({member_id}) added."
    except ValidationError as exc:
        db.session.rollback()
        return False, str(exc)


def _circulation_response(transaction, message, fine_amount=0, late_days=0):
    user = transaction.user
    copy = transaction.copy
    available = db.session.query(func.count(BookCopy.id)).outerjoin(
        Transaction,
        and_(Transaction.copy_id == BookCopy.copy_id, Transaction.return_date.is_(None)),
    ).filter(
        BookCopy.book_id == copy.book_id, Transaction.id.is_(None),
    ).scalar()
    return {
        "message": message,
        "student": _student_scan_details(user),
        "copy": _copy_scan_details(copy),
        "transaction": {
            "txn_id": transaction.id,
            "issue_date": transaction.issue_date.isoformat(),
            "due_date": transaction.due_date.isoformat(),
            "return_date": transaction.return_date.isoformat() if transaction.return_date else None,
        },
        "fine": fine_amount,
        "late_days": late_days,
        "available_copies": int(available or 0),
        "recent_transactions": recent_transactions(None, 10),
    }


def issue_book(member_id, copy_id):
    member_id = _required({"value": member_id}, "value", "Student ID or library card", 200)
    copy_id = _required({"value": copy_id}, "value", "Book QR code or copy ID", 200)
    if db.session().in_transaction():
        db.session.rollback()
    try:
        with db.session.begin():
            user = _member_by_scan(member_id)
            if user is None:
                raise NotFoundError("Student library card or ID was not recognized.")
            copy = _copy_by_scan(copy_id)
            if copy is None:
                raise NotFoundError("Book QR code or copy ID was not recognized.")
            if db.session.query(Transaction.id).filter_by(
                copy_id=copy.copy_id, return_date=None).first():
                raise ConflictError(f"{copy.book.title} ({copy.copy_id}) is already issued.")
            held = db.session.query(func.count(Transaction.id)).filter(
                Transaction.user_id == user.id,
                Transaction.return_date.is_(None),
            ).scalar()
            if held >= MAX_BOOKS:
                raise ConflictError(f"{user.name} already has {MAX_BOOKS} books (limit reached).")
            _advance_reservation_queue(copy.book_id)
            next_reservation = db.session.query(Reservation).filter_by(
                book_id=copy.book_id, status="READY").order_by(
                    Reservation.reserved_at, Reservation.id).first()
            if next_reservation and next_reservation.user_id != user.id:
                raise ConflictError(
                    f"This copy is reserved for {next_reservation.user.name}.")

            today = date.today()
            due = today + timedelta(days=LOAN_DAYS)
            transaction = Transaction(
                copy_id=copy.copy_id, user_id=user.id, issue_date=today, due_date=due)
            db.session.add(transaction)
            db.session.flush()
            _audit("ISSUE", "Transaction", transaction.id, {
                "copy_id": copy.copy_id, "member_id": user.member_id})
            db.session.add(Notification(
                user_id=user.id,
                transaction_id=transaction.id,
                kind="BOOK_ISSUED",
                message=f"{copy.book.title} is due {due.strftime('%d %b %Y')}.",
            ))
            if next_reservation and next_reservation.user_id == user.id:
                next_reservation.status = "FULFILLED"
                next_reservation.fulfilled_at = datetime.now()
            _advance_reservation_queue(copy.book_id)
            _refresh_reservations_for_student(user.id)
        message = (
            f"Issued '{copy.book.title}' to {user.name}. "
            f"Due {due.strftime('%d %b %Y')}.")
        return _circulation_response(transaction, message)
    except ValidationError:
        db.session.rollback()
        raise
    except IntegrityError as exc:
        db.session.rollback()
        raise ConflictError(
            "Issue could not be completed because the copy was checked out concurrently.") from exc
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise DatabaseError("The issue transaction could not be completed.") from exc


def scan_issue(_session, member_id, copy_id):
    try:
        result = issue_book(member_id, copy_id)
        return True, result["message"] + f" Copies left: {result['available_copies']}."
    except ValidationError as exc:
        return False, str(exc)


def return_copy(copy_id):
    copy_id = _required({"value": copy_id}, "value", "Book QR code or copy ID", 200)
    if db.session().in_transaction():
        db.session.rollback()
    try:
        with db.session.begin():
            copy = _copy_by_scan(copy_id)
            transaction = db.session.query(Transaction).filter(
                Transaction.copy_id == copy.copy_id if copy else False,
                Transaction.return_date.is_(None),
            ).with_for_update().first()
            if transaction is None:
                if copy is None:
                    raise NotFoundError("Book QR code or copy ID was not recognized.")
                raise ConflictError(f"Copy {copy.copy_id} is not currently issued.")
            today = date.today()
            late_days = max(0, (today - transaction.due_date).days)
            fine_amount = late_days * FINE_PER_DAY
            transaction.return_date = today
            if fine_amount:
                db.session.add(Fine(transaction=transaction, amount=fine_amount))
            _audit("RETURN", "Transaction", transaction.id, {
                "copy_id": transaction.copy_id, "fine": fine_amount})
            db.session.add(Notification(
                user_id=transaction.user_id,
                transaction_id=transaction.id,
                kind="BOOK_RETURNED",
                message=f"{transaction.copy.book.title} was returned."
                + (f" Fine due: Rs.{fine_amount}." if fine_amount else ""),
            ))
            title = transaction.copy.book.title
            returned_book_id = transaction.copy.book_id
            returned_user_id = transaction.user_id
            db.session.flush()
            _advance_reservation_queue(returned_book_id)
            _refresh_reservations_for_student(returned_user_id)
        note = f"Returned late by {late_days} day(s). Fine: Rs.{fine_amount}." if fine_amount else "Returned on time."
        response = _circulation_response(
            transaction, f"'{title}' returned. {note}", fine_amount, late_days)
        return response
    except ValidationError:
        db.session.rollback()
        raise
    except IntegrityError as exc:
        db.session.rollback()
        raise ConflictError(
            "Return could not be completed because the loan changed concurrently.") from exc
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise DatabaseError("The return transaction could not be completed.") from exc


def scan_return(_session, copy_id):
    try:
        result = return_copy(copy_id)
        return True, result["message"] + f" Copies available: {result['available_copies']}."
    except ValidationError as exc:
        return False, str(exc)


def create_book(data):
    title = _required(data, "title", "Title", 240)
    author = _required(data, "author", "Author", 180)
    category = _get_or_404(Category, data.get("category_id"), "Category")
    shelf = _get_or_404(Shelf, data.get("shelf_id"), "Shelf")
    if category.rack_id is not None and category.rack_id != shelf.rack_id:
        raise ValidationError("The selected shelf must belong to the category's assigned rack.")
    book = Book(
        title=title,
        author=author,
        category_id=category.id,
        shelf_id=shelf.id,
        cover_url=_optional(data, "cover_url", "Cover URL", 500),
    )
    db.session.add(book)
    _flush()
    _audit("CREATE", "Book", book.id, {"title": book.title})
    _commit()
    return book


def update_book(book_id, data):
    book = _get_or_404(Book, book_id, "Book")
    if "title" in data:
        book.title = _required(data, "title", "Title", 240)
    if "author" in data:
        book.author = _required(data, "author", "Author", 180)
    if "cover_url" in data:
        book.cover_url = _optional(data, "cover_url", "Cover URL", 500)
    if "category_id" in data:
        book.category = _get_or_404(Category, data["category_id"], "Category")
    if "shelf_id" in data:
        book.shelf = _get_or_404(Shelf, data["shelf_id"], "Shelf")
    if book.category.rack_id is not None and book.category.rack_id != book.shelf.rack_id:
        raise ValidationError("The selected shelf must belong to the category's assigned rack.")
    _audit("UPDATE", "Book", book.id, {"title": book.title})
    _commit()
    return book


def delete_book(book_id):
    book = _get_or_404(Book, book_id, "Book")
    db.session.delete(book)
    _audit("DELETE", "Book", book.id)
    _commit()


def create_copy(book_id, data):
    book = _get_or_404(Book, book_id, "Book")
    copy_id = _required(data, "copy_id", "Copy ID", 80).upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]*", copy_id):
        raise ValidationError("Copy ID may contain only letters, numbers, dots, underscores, and hyphens.")
    qr_code = _optional(data, "qr_code", "QR code", 200) or copy_qr_payload(copy_id)
    copy = BookCopy(copy_id=copy_id, qr_code=qr_code, book_id=book.id)
    db.session.add(copy)
    _flush()
    _audit("CREATE", "BookCopy", copy.copy_id, {"book_id": book.id})
    _advance_reservation_queue(book.id)
    _commit()
    return copy


def delete_copy(copy_id):
    copy = db.session.query(BookCopy).filter_by(copy_id=copy_id.upper()).first()
    if copy is None:
        raise NotFoundError("Book copy was not found.")
    db.session.delete(copy)
    _audit("DELETE", "BookCopy", copy.copy_id)
    _commit()


def serialize_book(book):
    summary = get_book(None, book.id)
    return summary


def list_members():
    return members_with_counts()


def serialize_member(user):
    return {
        "id": user.id,
        "member_id": user.member_id,
        "qr_code": user.qr_code,
        "name": user.name,
        "phone": user.phone,
        "role": user.role,
    }


def create_member(data):
    member_id = _required(data, "member_id", "Member ID", 64).upper()
    name = _required(data, "name", "Name", 160)
    phone = _optional(data, "phone", "Phone", 40)
    if _member_by_code(member_id):
        raise ConflictError(f"Member ID {member_id} already exists.")
    user = User(member_id=member_id, name=name, phone=phone, role="STUDENT")
    user.qr_code = student_qr_payload(member_id)
    db.session.add(user)
    _flush()
    _audit("CREATE", "User", user.id, {"member_id": member_id})
    _commit()
    return user


def update_member(member_id, data):
    user = _member_by_code(member_id)
    if user is None:
        raise NotFoundError("Member was not found.")
    if "member_id" in data:
        user.member_id = _required(data, "member_id", "Member ID", 64).upper()
        user.qr_code = student_qr_payload(user.member_id)
    if "name" in data:
        user.name = _required(data, "name", "Name", 160)
    if "phone" in data:
        user.phone = _optional(data, "phone", "Phone", 40)
    _audit("UPDATE", "User", user.id, {"member_id": user.member_id})
    _commit()
    return user


def delete_member(member_id):
    user = _member_by_code(member_id)
    if user is None:
        raise NotFoundError("Member was not found.")
    db.session.delete(user)
    _audit("DELETE", "User", user.id, {"member_id": user.member_id})
    _commit()


def serialize_category(category):
    return {
        "id": category.id,
        "name": category.name,
        "description": category.description,
        "rack_id": category.rack_id,
        "color_start": category.color_start,
        "color_end": category.color_end,
    }


def list_categories():
    return [serialize_category(category) for category in
            db.session.query(Category).order_by(Category.name).all()]


def create_category(data):
    name = _required(data, "name", "Category name", 120)
    rack_id = _identifier(data.get("rack_id"), "Rack ID")
    _get_or_404(Rack, rack_id, "Rack")
    category = Category(
        name=name,
        description=_optional(data, "description", "Description", 2000),
        rack_id=rack_id,
        color_start=_color(data, "color_start", "#315e53"),
        color_end=_color(data, "color_end", "#2dd4bf"),
    )
    db.session.add(category)
    _flush()
    _audit("CREATE", "Category", category.id, {"name": category.name})
    _commit()
    return category


def update_category(category_id, data):
    category = _get_or_404(Category, category_id, "Category")
    if "name" in data:
        category.name = _required(data, "name", "Category name", 120)
    if "description" in data:
        category.description = _optional(data, "description", "Description", 2000)
    if "rack_id" in data:
        rack_id = _identifier(data["rack_id"], "Rack ID")
        _get_or_404(Rack, rack_id, "Rack")
        if any(book.shelf.rack_id != rack_id for book in category.books):
            raise ValidationError("Category rack cannot change while its books are assigned to another rack.")
        category.rack_id = rack_id
    if "color_start" in data:
        category.color_start = _color(data, "color_start", category.color_start)
    if "color_end" in data:
        category.color_end = _color(data, "color_end", category.color_end)
    _audit("UPDATE", "Category", category.id, {"name": category.name})
    _commit()
    return category


def delete_category(category_id):
    category = _get_or_404(Category, category_id, "Category")
    db.session.delete(category)
    _audit("DELETE", "Category", category.id, {"name": category.name})
    _commit()


def serialize_rack(rack, include_shelves=True):
    data = {
        "id": rack.id,
        "rack_code": rack.rack_code,
        "name": rack.name,
        "location": rack.location,
    }
    if include_shelves:
        data["shelves"] = [
            {"id": shelf.id, "shelf_number": shelf.shelf_number, "name": shelf.name}
            for shelf in sorted(rack.shelves, key=lambda item: item.shelf_number)
        ]
    return data


def list_racks():
    return [serialize_rack(rack) for rack in
            db.session.query(Rack).order_by(Rack.rack_code).all()]


def create_rack(data):
    code = _required(data, "rack_code", "Rack code", 32).upper()
    rack = Rack(
        rack_code=code,
        name=_required(data, "name", "Rack name", 120) if data.get("name") else f"Rack {code}",
        location=_optional(data, "location", "Location", 160),
    )
    db.session.add(rack)
    _flush()
    _audit("CREATE", "Rack", rack.id, {"rack_code": rack.rack_code})
    _commit()
    return rack


def update_rack(rack_id, data):
    rack = _get_or_404(Rack, rack_id, "Rack")
    if "rack_code" in data:
        rack.rack_code = _required(data, "rack_code", "Rack code", 32).upper()
    if "name" in data:
        rack.name = _required(data, "name", "Rack name", 120)
    if "location" in data:
        rack.location = _optional(data, "location", "Location", 160)
    _audit("UPDATE", "Rack", rack.id, {"rack_code": rack.rack_code})
    _commit()
    return rack


def delete_rack(rack_id):
    rack = _get_or_404(Rack, rack_id, "Rack")
    db.session.delete(rack)
    _audit("DELETE", "Rack", rack.id, {"rack_code": rack.rack_code})
    _commit()


def create_shelf(rack_id, data):
    rack = _get_or_404(Rack, rack_id, "Rack")
    number = _identifier(data.get("shelf_number"), "Shelf number")
    shelf = Shelf(
        rack_id=rack.id,
        shelf_number=number,
        name=_optional(data, "name", "Shelf name", 120),
    )
    db.session.add(shelf)
    _flush()
    _audit("CREATE", "Shelf", shelf.id, {"rack_id": rack.id, "shelf_number": number})
    _commit()
    return shelf


def update_shelf(shelf_id, data):
    shelf = _get_or_404(Shelf, shelf_id, "Shelf")
    if "rack_id" in data:
        rack_id = _identifier(data["rack_id"], "Rack ID")
        _get_or_404(Rack, rack_id, "Rack")
        if any(book.category.rack_id not in (None, rack_id) for book in shelf.books):
            raise ValidationError("Shelf rack cannot change while its books are assigned to another category rack.")
        shelf.rack_id = rack_id
    if "shelf_number" in data:
        shelf.shelf_number = _identifier(data["shelf_number"], "Shelf number")
    if "name" in data:
        shelf.name = _optional(data, "name", "Shelf name", 120)
    _audit("UPDATE", "Shelf", shelf.id, {
        "rack_id": shelf.rack_id, "shelf_number": shelf.shelf_number})
    _commit()
    return shelf


def delete_shelf(shelf_id):
    shelf = _get_or_404(Shelf, shelf_id, "Shelf")
    db.session.delete(shelf)
    _audit("DELETE", "Shelf", shelf.id, {"shelf_number": shelf.shelf_number})
    _commit()


def serialize_shelf(shelf):
    return {
        "id": shelf.id,
        "rack_id": shelf.rack_id,
        "rack_code": shelf.rack.rack_code,
        "shelf_number": shelf.shelf_number,
        "name": shelf.name,
    }


def shelves_for_rack(rack_id):
    rack = _get_or_404(Rack, rack_id, "Rack")
    return [serialize_shelf(shelf) for shelf in
            sorted(rack.shelves, key=lambda item: item.shelf_number)]


def get_shelf(shelf_id):
    return serialize_shelf(_get_or_404(Shelf, shelf_id, "Shelf"))
