import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from flask import Flask
from sqlalchemy import inspect
from werkzeug.security import check_password_hash

import db as library_db
from models import (
    ActivityLog, Book, BookCopy, Category, Fine, Notification, Rack, Reservation, SchemaMigration,
    Shelf, Transaction, User,
)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = Flask("library-database-test")
        database_path = os.path.join(self.directory.name, "library.db").replace("\\", "/")
        self.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database_path}"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        self.app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 30}}
        library_db.db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        library_db.init_db()

    def tearDown(self):
        library_db.db.session.remove()
        library_db.db.engine.dispose()
        self.context.pop()
        self.directory.cleanup()

    def test_empty_database_is_not_seeded(self):
        self.assertEqual(library_db.stats()["titles"], 0)
        self.assertEqual(library_db.stats()["copies"], 0)
        self.assertEqual(library_db.stats()["members"], 0)
        self.assertEqual(
            set(inspect(library_db.db.engine).get_table_names()),
            {
                "activity_logs", "book_copies", "books", "categories", "fines",
                "notifications", "racks", "reservations", "schema_migrations",
                "shelves", "transactions", "users",
            },
        )

    def test_relations_validation_issue_return_and_fines(self):
        rack = library_db.create_rack({"rack_code": "a", "name": "Rack A"})
        shelf = library_db.create_shelf(rack.id, {"shelf_number": 1})
        category = library_db.create_category({"name": "Science", "rack_id": rack.id})
        book = library_db.create_book({
            "title": "Test Book", "author": "A. Writer",
            "category_id": category.id, "shelf_id": shelf.id,
        })
        member = library_db.create_member({"member_id": "s-1", "name": "Student One"})
        copy = library_db.create_copy(book.id, {"copy_id": "bk-1-c1"})

        self.assertEqual(copy.qr_code, "SMARTLIBRARY:COPY:BK-1-C1")
        self.assertTrue(library_db.scan_issue(None, member.member_id, copy.qr_code)[0])
        self.assertEqual(library_db.get_book(None, book.id)["available"], 0)
        self.assertFalse(library_db.scan_issue(None, member.member_id, copy.copy_id)[0])
        transaction = library_db.db.session.query(Transaction).one()
        transaction.due_date = date.today() - timedelta(days=3)
        transaction.issue_date = transaction.due_date - timedelta(days=14)
        library_db.db.session.commit()

        self.assertTrue(library_db.scan_return(None, copy.qr_code)[0])
        self.assertEqual(library_db.get_book(None, book.id)["available"], 1)
        self.assertEqual(library_db.db.session.query(Fine).one().amount, 6)
        self.assertEqual(library_db.db.session.query(Notification).count(), 2)
        self.assertEqual(library_db.db.session.query(ActivityLog).count(), 8)
        self.assertEqual(library_db.db.session.query(Book).one().category.name, "Science")
        self.assertEqual(library_db.db.session.query(BookCopy).one().status, "AVAILABLE")
        self.assertEqual(library_db.db.session.query(User).one().member_id, "S-1")

    def test_qr_lookup_issue_return_and_overdue_fine(self):
        rack = library_db.create_rack({"rack_code": "Q", "name": "QR Rack"})
        shelf = library_db.create_shelf(rack.id, {"shelf_number": 1})
        category = library_db.create_category({"name": "QR Tests", "rack_id": rack.id})
        book = library_db.create_book({
            "title": "Scannable Book", "author": "A. Writer",
            "category_id": category.id, "shelf_id": shelf.id,
        })
        member = library_db.create_member({"member_id": "qr-1", "name": "QR Student"})
        copy = library_db.create_copy(book.id, {"copy_id": "qr-copy-1"})

        student_scan = library_db.lookup_scan(member.qr_code, "issue")
        self.assertEqual(student_scan["kind"], "student")
        self.assertTrue(student_scan["student"]["eligible"])
        book_scan = library_db.lookup_scan(
            copy.qr_code, "issue", member_id=student_scan["student"]["member_id"])
        self.assertTrue(book_scan["copy"]["available"])

        issue = library_db.issue_book(member.qr_code, copy.qr_code)
        self.assertEqual(issue["transaction"]["due_date"], (date.today() + timedelta(days=14)).isoformat())
        self.assertEqual(issue["copy"]["copy_id"], copy.copy_id)
        with self.assertRaises(library_db.ConflictError):
            library_db.issue_book(member.member_id, copy.copy_id)

        active_loan = library_db.lookup_scan(copy.qr_code, "return")
        self.assertEqual(active_loan["student"]["member_id"], member.member_id)
        transaction = library_db.db.session.query(Transaction).one()
        transaction.due_date = date.today() - timedelta(days=2)
        transaction.issue_date = transaction.due_date - timedelta(days=14)
        library_db.db.session.commit()

        returned = library_db.return_copy(copy.copy_id)
        self.assertEqual(returned["fine"], 4)
        self.assertEqual(returned["late_days"], 2)
        self.assertEqual(returned["copy"]["available"], True)
        self.assertEqual(returned["recent_transactions"][0]["return_date"], date.today().isoformat())
        with self.assertRaises(library_db.ConflictError):
            library_db.return_copy(copy.qr_code)
        with self.assertRaises(library_db.NotFoundError):
            library_db.lookup_scan("not-a-student", "issue")

    def test_borrowing_limit_is_enforced_by_scan_workflow(self):
        rack = library_db.create_rack({"rack_code": "L", "name": "Limit Rack"})
        shelf = library_db.create_shelf(rack.id, {"shelf_number": 1})
        category = library_db.create_category({"name": "Limit Tests", "rack_id": rack.id})
        book = library_db.create_book({
            "title": "Limit Book", "author": "A. Writer",
            "category_id": category.id, "shelf_id": shelf.id,
        })
        member = library_db.create_member({"member_id": "limit-1", "name": "Limit Student"})
        copies = [
            library_db.create_copy(book.id, {"copy_id": f"limit-copy-{number}"})
            for number in range(1, library_db.MAX_BOOKS + 2)
        ]
        for copy in copies[:library_db.MAX_BOOKS]:
            library_db.issue_book(member.member_id, copy.copy_id)
        student_scan = library_db.lookup_scan(member.qr_code, "issue")
        self.assertFalse(student_scan["student"]["eligible"])
        with self.assertRaises(library_db.ConflictError):
            library_db.issue_book(member.member_id, copies[-1].copy_id)

    def test_reservation_queue_is_time_ordered_and_notifies_next_student(self):
        rack = library_db.create_rack({"rack_code": "R", "name": "Reservation Rack"})
        shelf = library_db.create_shelf(rack.id, {"shelf_number": 1})
        category = library_db.create_category({"name": "Reservation Tests", "rack_id": rack.id})
        book = library_db.create_book({
            "title": "Reserved Book", "author": "A. Writer",
            "category_id": category.id, "shelf_id": shelf.id,
        })
        borrower = library_db.create_member({"member_id": "borrower", "name": "Current Borrower"})
        first = library_db.create_member({"member_id": "first", "name": "First Student"})
        second = library_db.create_member({"member_id": "second", "name": "Second Student"})
        copy = library_db.create_copy(book.id, {"copy_id": "reserved-copy"})
        library_db.issue_book(borrower.member_id, copy.copy_id)
        first_reservation = library_db.create_reservation(first.member_id, book.id)
        second_reservation = library_db.create_reservation(second.member_id, book.id)
        self.assertEqual(first_reservation["status"], "PENDING")
        self.assertEqual(second_reservation["queue_position"], 2)
        with self.assertRaises(library_db.ConflictError):
            library_db.create_reservation(first.member_id, book.id)

        library_db.return_copy(copy.copy_id)
        first_row = library_db.db.session.get(Reservation, first_reservation["id"])
        second_row = library_db.db.session.get(Reservation, second_reservation["id"])
        self.assertEqual(first_row.status, "READY")
        self.assertEqual(second_row.status, "PENDING")
        with self.assertRaises(library_db.ConflictError):
            library_db.issue_book(second.member_id, copy.copy_id)
        library_db.issue_book(first.member_id, copy.copy_id)
        self.assertEqual(first_row.status, "FULFILLED")
        library_db.return_copy(copy.copy_id)
        self.assertEqual(second_row.status, "READY")
        notices = library_db.db.session.query(Notification).filter(
            Notification.kind == "RESERVATION_READY").all()
        self.assertEqual({notice.user_id for notice in notices}, {first.id, second.id})

    def test_due_notifications_are_idempotent_and_passwords_are_hashed(self):
        rack = library_db.create_rack({"rack_code": "N", "name": "Notification Rack"})
        shelf = library_db.create_shelf(rack.id, {"shelf_number": 1})
        category = library_db.create_category({"name": "Notification Tests", "rack_id": rack.id})
        book = library_db.create_book({
            "title": "Reminder Book", "author": "A. Writer",
            "category_id": category.id, "shelf_id": shelf.id,
        })
        member = library_db.create_member({"member_id": "notice-1", "name": "Notice Student"})
        copy = library_db.create_copy(book.id, {"copy_id": "notice-copy"})
        library_db.issue_book(member.member_id, copy.copy_id)
        transaction = library_db.db.session.query(Transaction).one()
        transaction.due_date = date.today() + timedelta(days=2)
        library_db.db.session.commit()

        self.assertEqual(library_db.generate_due_notifications(member.member_id), 1)
        self.assertEqual(library_db.generate_due_notifications(member.member_id), 0)
        self.assertEqual(
            library_db.db.session.query(Notification).filter_by(kind="DUE_SOON").count(), 1)
        transaction.due_date = date.today() - timedelta(days=1)
        transaction.issue_date = transaction.due_date - timedelta(days=14)
        library_db.db.session.commit()
        self.assertEqual(library_db.generate_due_notifications(member.member_id), 1)
        self.assertEqual(
            library_db.db.session.query(Notification).filter_by(kind="OVERDUE").count(), 1)

        library_db.set_member_password(member.member_id, "correct horse battery staple")
        self.assertNotEqual(member.password_hash, "correct horse battery staple")
        self.assertTrue(check_password_hash(member.password_hash, "correct horse battery staple"))
        with self.assertRaises(library_db.ValidationError):
            library_db.set_member_password(member.member_id, "short")

    def test_floor_map_reports_real_copy_locations_and_search_matches(self):
        rack = library_db.create_rack({"rack_code": "MAP", "name": "Map Rack"})
        shelf = library_db.create_shelf(rack.id, {"shelf_number": 4})
        category = library_db.create_category({"name": "Map Category", "rack_id": rack.id})
        book = library_db.create_book({
            "title": "Location Search Title", "author": "Map Author",
            "category_id": category.id, "shelf_id": shelf.id,
        })
        copy = library_db.create_copy(book.id, {"copy_id": "map-copy"})
        matches = library_db.floor_map_data("Location Search")
        rack_data = next(item for item in matches if item["rack_code"] == "MAP")
        self.assertEqual(rack_data["copies"], 1)
        self.assertEqual(rack_data["available"], 1)
        self.assertEqual(rack_data["shelves"][0]["number"], 4)
        self.assertTrue(rack_data["matches"])
        self.assertTrue(rack_data["shelves"][0]["books"][0]["matches"])
        self.assertEqual(rack_data["shelves"][0]["books"][0]["copies"][0]["copy_id"], copy.copy_id)

    def test_analytics_and_report_data_match_real_circulation(self):
        rack = library_db.create_rack({"rack_code": "REP", "name": "Report Rack"})
        shelf = library_db.create_shelf(rack.id, {"shelf_number": 2})
        category = library_db.create_category({"name": "Report Category", "rack_id": rack.id})
        book = library_db.create_book({
            "title": "Analytics Book", "author": "A. Writer",
            "category_id": category.id, "shelf_id": shelf.id,
        })
        first_member = library_db.create_member({
            "member_id": "report-1", "name": "Report Student One"})
        second_member = library_db.create_member({
            "member_id": "report-2", "name": "Report Student Two"})
        returned_copy = library_db.create_copy(book.id, {"copy_id": "report-copy-1"})
        overdue_copy = library_db.create_copy(book.id, {"copy_id": "report-copy-2"})
        library_db.issue_book(first_member.member_id, returned_copy.copy_id)
        library_db.issue_book(second_member.member_id, overdue_copy.copy_id)

        today = date.today()
        first_day = today.replace(day=1)
        transactions = library_db.db.session.query(Transaction).order_by(Transaction.id).all()
        transactions[0].issue_date = first_day
        transactions[0].due_date = first_day + timedelta(days=14)
        transactions[0].return_date = today
        transactions[1].issue_date = today - timedelta(days=15)
        transactions[1].due_date = today - timedelta(days=1)
        library_db.db.session.commit()

        analytics = library_db.analytics_data()
        current_month = today.strftime("%Y-%m")
        current_index = analytics["months"].index(current_month)
        self.assertEqual(len(analytics["months"]), 12)
        self.assertEqual(analytics["monthly_issues"][current_index], 1)
        self.assertEqual(analytics["monthly_returns"][current_index], 1)
        self.assertEqual(analytics["most_borrowed"][0]["borrow_count"], 2)
        self.assertEqual(analytics["active_students"][0]["borrow_count"], 1)
        self.assertEqual(analytics["category_borrowing"][0]["borrow_count"], 2)
        self.assertEqual(analytics["overdue"], {"count": 1, "estimated_fines": library_db.FINE_PER_DAY})
        self.assertEqual(analytics["inventory"]["available"], 1)
        self.assertEqual(analytics["inventory"]["issued"], 1)

        member_report = library_db.member_borrowing_report(first_member.member_id)
        self.assertEqual(member_report["history"][0]["status"], "Returned")
        self.assertEqual(member_report["history"][0]["copy_id"], returned_copy.copy_id)
        self.assertIsNone(library_db.member_borrowing_report("missing-student"))
        inventory = library_db.inventory_report()
        self.assertEqual({row["copy_id"]: row["status"] for row in inventory}, {
            "REPORT-COPY-1": "Available", "REPORT-COPY-2": "Issued",
        })
        self.assertEqual(len(library_db.overdue_list()), 1)

    def test_duplicate_ids_and_referenced_deletes_are_rejected(self):
        rack = library_db.create_rack({"rack_code": "B"})
        shelf = library_db.create_shelf(rack.id, {"shelf_number": 1})
        category = library_db.create_category({"name": "Math", "rack_id": rack.id})
        book = library_db.create_book({
            "title": "Numbers", "author": "A. Writer",
            "category_id": category.id, "shelf_id": shelf.id,
        })
        library_db.create_copy(book.id, {"copy_id": "unique-copy"})
        with self.assertRaises(library_db.ConflictError):
            library_db.create_copy(book.id, {"copy_id": "UNIQUE-COPY"})
        with self.assertRaises(library_db.ValidationError):
            library_db.create_book({"title": "", "author": "A", "category_id": category.id,
                                    "shelf_id": shelf.id})
        with self.assertRaises(library_db.ConflictError):
            library_db.delete_category(category.id)

    def test_legacy_records_migrate_once_and_remain_archived(self):
        legacy_path = os.path.join(self.directory.name, "legacy.db")
        connection = sqlite3.connect(legacy_path)
        connection.executescript("""
            CREATE TABLE books (
                book_id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                author TEXT NOT NULL, category TEXT NOT NULL, rack TEXT NOT NULL,
                shelf_row INTEGER NOT NULL, cover_url TEXT
            );
            CREATE TABLE copies (
                copy_id TEXT PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(book_id),
                status TEXT NOT NULL DEFAULT 'AVAILABLE'
            );
            CREATE TABLE members (
                member_id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT
            );
            CREATE TABLE transactions (
                txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                copy_id TEXT NOT NULL REFERENCES copies(copy_id),
                member_id TEXT NOT NULL REFERENCES members(member_id),
                issue_date TEXT NOT NULL, due_date TEXT NOT NULL,
                return_date TEXT, fine INTEGER DEFAULT 0
            );
            INSERT INTO books VALUES (7, 'Legacy Title', 'Legacy Author', 'History', 'H', 2, NULL);
            INSERT INTO copies VALUES ('BK007-C1', 7, 'ISSUED');
            INSERT INTO members VALUES ('M7', 'Legacy Member', '5550100');
            INSERT INTO transactions VALUES (9, 'BK007-C1', 'M7', '2026-10-01', '2026-10-15', NULL, 0);
        """)
        connection.commit()
        connection.close()

        library_db.db.session.remove()
        library_db.db.engine.dispose()
        self.context.pop()
        self.app = Flask("legacy-library-database-test")
        self.app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{legacy_path.replace(chr(92), '/')}"
        self.app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        self.app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"connect_args": {"timeout": 30}}
        library_db.db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        library_db.init_db()

        book = library_db.db.session.get(Book, 7)
        copy = library_db.db.session.query(BookCopy).one()
        self.assertEqual(book.title, "Legacy Title")
        self.assertEqual(book.category.name, "History")
        self.assertEqual(book.shelf.rack.rack_code, "H")
        self.assertEqual(copy.qr_code, "SMARTLIBRARY:COPY:BK007-C1")
        self.assertEqual(copy.status, "ISSUED")
        self.assertEqual(library_db.db.session.query(Transaction).one().id, 9)
        migrated_user = library_db.db.session.query(User).one()
        self.assertEqual(migrated_user.member_id, "M7")
        self.assertEqual(migrated_user.qr_code, "SMARTLIBRARY:STUDENT:M7")
        self.assertIsNotNone(library_db.db.session.get(SchemaMigration, 1))
        self.assertIsNotNone(library_db.db.session.get(SchemaMigration, 2))
        self.assertIsNotNone(library_db.db.session.get(SchemaMigration, 3))
        self.assertIn("legacy_v1_books", inspect(library_db.db.engine).get_table_names())

        library_db.init_db()
        self.assertEqual(library_db.db.session.query(Book).count(), 1)
        self.assertEqual(library_db.db.session.query(Transaction).count(), 1)

    def test_v1_schema_gets_student_qr_codes_without_data_loss(self):
        v1_path = os.path.join(self.directory.name, "v1.db")
        connection = sqlite3.connect(v1_path)
        connection.executescript("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY, member_id VARCHAR(64) NOT NULL UNIQUE,
                name VARCHAR(160) NOT NULL, phone VARCHAR(40),
                role VARCHAR(24) NOT NULL, created_at DATETIME NOT NULL
            );
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL
            );
            INSERT INTO users VALUES (1, 'OLD-1', 'Existing Student', NULL, 'STUDENT', '2026-01-01');
            INSERT INTO schema_migrations VALUES (1, '2026-01-01');
        """)
        connection.commit()
        connection.close()

        upgrade_app = Flask("v1-library-database-test")
        upgrade_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{v1_path.replace(chr(92), '/')}"
        upgrade_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
        library_db.db.init_app(upgrade_app)
        with upgrade_app.app_context():
            library_db.init_db()
            user = library_db.db.session.get(User, 1)
            self.assertEqual(user.name, "Existing Student")
            self.assertEqual(user.qr_code, "SMARTLIBRARY:STUDENT:OLD-1")
            self.assertIsNotNone(library_db.db.session.get(SchemaMigration, 2))
            self.assertIsNotNone(library_db.db.session.get(SchemaMigration, 3))
            self.assertIsNone(user.password_hash)
            library_db.db.session.remove()
            library_db.db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
