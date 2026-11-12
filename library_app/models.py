import sqlite3
import re
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Engine, Index, event, text
from sqlalchemy.orm import validates


db = SQLAlchemy()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(connection, _record):
    if isinstance(connection, sqlite3.Connection):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def _required_text(value, field, max_length):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required.")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"{field} must be {max_length} characters or fewer.")
    return value


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    qr_code = db.Column(db.String(200), nullable=False, unique=True, index=True)
    name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(254))
    phone = db.Column(db.String(40))
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(24), nullable=False, default="STUDENT")
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    transactions = db.relationship("Transaction", back_populates="user", passive_deletes=True)
    reservations = db.relationship("Reservation", back_populates="user", passive_deletes=True)
    notifications = db.relationship("Notification", back_populates="user", passive_deletes=True)

    @validates("member_id")
    def validate_member_id(self, _key, value):
        return _required_text(value, "Member ID", 64).upper()

    @validates("name")
    def validate_name(self, _key, value):
        return _required_text(value, "Name", 160)

    @validates("email")
    def validate_email(self, _key, value):
        if value is None:
            return None
        email = _required_text(value, "Email", 254).lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("Email must be a valid email address.")
        return email

    @validates("role")
    def validate_role(self, _key, value):
        role = _required_text(value, "Role", 24).upper()
        if role not in {"STUDENT", "LIBRARIAN", "ADMIN"}:
            raise ValueError("Role must be STUDENT, LIBRARIAN, or ADMIN.")
        return role


class Rack(db.Model):
    __tablename__ = "racks"

    id = db.Column(db.Integer, primary_key=True)
    rack_code = db.Column(db.String(32), nullable=False, unique=True)
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(160))
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    shelves = db.relationship("Shelf", back_populates="rack", passive_deletes=True)
    categories = db.relationship("Category", back_populates="rack", passive_deletes=True)

    @validates("rack_code")
    def validate_rack_code(self, _key, value):
        return _required_text(value, "Rack code", 32).upper()

    @validates("name")
    def validate_name(self, _key, value):
        return _required_text(value, "Rack name", 120)


class Shelf(db.Model):
    __tablename__ = "shelves"
    __table_args__ = (
        db.UniqueConstraint("rack_id", "shelf_number", name="uq_shelf_rack_number"),
        db.CheckConstraint("shelf_number > 0", name="ck_shelf_number_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    rack_id = db.Column(db.Integer, db.ForeignKey("racks.id", ondelete="RESTRICT"), nullable=False)
    shelf_number = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(120))

    rack = db.relationship("Rack", back_populates="shelves")
    books = db.relationship("Book", back_populates="shelf", passive_deletes=True)


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.Text)
    rack_id = db.Column(db.Integer, db.ForeignKey("racks.id", ondelete="RESTRICT"))
    color_start = db.Column(db.String(7), nullable=False, default="#315e53")
    color_end = db.Column(db.String(7), nullable=False, default="#2dd4bf")
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    rack = db.relationship("Rack", back_populates="categories")
    books = db.relationship("Book", back_populates="category", passive_deletes=True)

    @validates("name")
    def validate_name(self, _key, value):
        return _required_text(value, "Category name", 120)


class Book(db.Model):
    __tablename__ = "books"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(240), nullable=False, index=True)
    author = db.Column(db.String(180), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False)
    shelf_id = db.Column(db.Integer, db.ForeignKey("shelves.id", ondelete="RESTRICT"), nullable=False)
    cover_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    category = db.relationship("Category", back_populates="books")
    shelf = db.relationship("Shelf", back_populates="books")
    copies = db.relationship("BookCopy", back_populates="book", passive_deletes=True)

    @validates("title")
    def validate_title(self, _key, value):
        return _required_text(value, "Title", 240)

    @validates("author")
    def validate_author(self, _key, value):
        return _required_text(value, "Author", 180)

    @property
    def rack(self):
        return self.shelf.rack.rack_code

    @property
    def shelf_row(self):
        return self.shelf.shelf_number


class BookCopy(db.Model):
    __tablename__ = "book_copies"

    id = db.Column(db.Integer, primary_key=True)
    copy_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    qr_code = db.Column(db.String(200), nullable=False, unique=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="RESTRICT"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    book = db.relationship("Book", back_populates="copies")
    transactions = db.relationship("Transaction", back_populates="copy", passive_deletes=True)

    @validates("copy_id")
    def validate_copy_id(self, _key, value):
        copy_id = _required_text(value, "Copy ID", 80).upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]*", copy_id):
            raise ValueError("Copy ID may contain only letters, numbers, dots, underscores, and hyphens.")
        return copy_id

    @validates("qr_code")
    def validate_qr_code(self, _key, value):
        return _required_text(value, "QR code", 200)

    @property
    def status(self):
        return "ISSUED" if any(tx.return_date is None for tx in self.transactions) else "AVAILABLE"


class Transaction(db.Model):
    __tablename__ = "transactions"
    __table_args__ = (
        Index(
            "uq_transaction_active_copy",
            "copy_id",
            unique=True,
            sqlite_where=text("return_date IS NULL"),
        ),
        db.CheckConstraint("due_date >= issue_date", name="ck_transaction_due_after_issue"),
        db.CheckConstraint(
            "return_date IS NULL OR return_date >= issue_date",
            name="ck_transaction_return_after_issue",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    copy_id = db.Column(db.String(80), db.ForeignKey("book_copies.copy_id", ondelete="RESTRICT"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    issue_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=False, index=True)
    return_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    copy = db.relationship("BookCopy", back_populates="transactions")
    user = db.relationship("User", back_populates="transactions")
    fine_record = db.relationship("Fine", back_populates="transaction", uselist=False, passive_deletes=True)

    @property
    def fine(self):
        return self.fine_record.amount if self.fine_record else 0


class Reservation(db.Model):
    __tablename__ = "reservations"
    __table_args__ = (
        Index(
            "uq_reservation_active_user_book",
            "user_id",
            "book_id",
            unique=True,
            sqlite_where=text("status IN ('PENDING', 'READY')"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    book_id = db.Column(db.Integer, db.ForeignKey("books.id", ondelete="RESTRICT"), nullable=False, index=True)
    reserved_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    status = db.Column(db.String(24), nullable=False, default="PENDING")
    fulfilled_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="reservations")
    book = db.relationship("Book")

    @validates("status")
    def validate_status(self, _key, value):
        status = _required_text(value, "Reservation status", 24).upper()
        if status not in {"PENDING", "READY", "FULFILLED", "CANCELLED"}:
            raise ValueError("Invalid reservation status.")
        return status


class Fine(db.Model):
    __tablename__ = "fines"
    __table_args__ = (
        db.CheckConstraint("amount >= 0", name="ck_fine_amount_nonnegative"),
        db.CheckConstraint("paid_amount >= 0 AND paid_amount <= amount", name="ck_fine_payment_range"),
    )

    id = db.Column(db.Integer, primary_key=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False, unique=True)
    amount = db.Column(db.Integer, nullable=False)
    paid_amount = db.Column(db.Integer, nullable=False, default=0)
    assessed_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    paid_at = db.Column(db.DateTime)

    transaction = db.relationship("Transaction", back_populates="fine_record")


class Notification(db.Model):
    __tablename__ = "notifications"
    __table_args__ = (
        db.UniqueConstraint(
            "transaction_id", "kind", name="uq_notification_transaction_kind"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    transaction_id = db.Column(
        db.Integer, db.ForeignKey("transactions.id", ondelete="CASCADE"), index=True)
    kind = db.Column(db.String(32), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    read_at = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="notifications")
    transaction = db.relationship("Transaction")

    @validates("kind", "message")
    def validate_text(self, key, value):
        limit = 32 if key == "kind" else 2000
        return _required_text(value, key.replace("_", " ").capitalize(), limit)


class ActivityLog(db.Model):
    __tablename__ = "activity_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    action = db.Column(db.String(64), nullable=False)
    entity_type = db.Column(db.String(64), nullable=False)
    entity_id = db.Column(db.String(80))
    details = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow, index=True)

    @validates("action", "entity_type")
    def validate_text(self, key, value):
        return _required_text(value, key.replace("_", " ").capitalize(), 64)


class SchemaMigration(db.Model):
    __tablename__ = "schema_migrations"

    version = db.Column(db.Integer, primary_key=True)
    applied_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
