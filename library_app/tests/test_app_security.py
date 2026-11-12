import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class FlaskSecurityIntegrationTest(unittest.TestCase):
    def test_role_guards_csrf_student_portal_and_scan_api(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment.update({
                "LIBRARY_DB": str(Path(directory) / "security-test.db"),
                "LIBRARY_SECRET_KEY": "isolated-test-session-secret",
                "LIBRARY_ADMIN_ID": "ADMIN-TEST",
                "LIBRARY_ADMIN_NAME": "Test Administrator",
                "LIBRARY_ADMIN_PASSWORD": "secure-test-admin-password",
            })
            script = r'''
import re
from datetime import date, timedelta
import app as app_module
import db as library_db
from models import Book, Notification, Transaction, User

application = app_module.app
application.config.update(TESTING=True)
with application.app_context():
    rack = library_db.create_rack({"rack_code": "SEC", "name": "Security Rack"})
    shelf = library_db.create_shelf(rack.id, {"shelf_number": 1})
    category = library_db.create_category({"name": "Security Tests", "rack_id": rack.id})
    book = library_db.create_book({
        "title": "Portal Security Book", "author": "A. Writer",
        "category_id": category.id, "shelf_id": shelf.id,
    })
    library_db.create_copy(book.id, {"copy_id": "SEC-COPY-1"})
    student = library_db.create_member({
        "member_id": "STUDENT-TEST", "name": "Test Student"})
    library_db.set_member_password(student.member_id, "secure-test-student-password")
    student_id, book_id = student.member_id, book.id

def csrf(response):
    token = re.search(rb'<meta name="csrf-token" content="([^"]+)"', response.data)
    assert token, "CSRF token is missing from the page"
    return token.group(1).decode()

anonymous = application.test_client()
assert anonymous.get("/scan").status_code == 302
assert anonymous.get("/reports").status_code == 302
assert anonymous.get("/api/analytics").status_code == 401
assert anonymous.get("/api/members").status_code == 401
assert anonymous.get("/api/books").status_code == 200
assert anonymous.post("/api/books", json={}).status_code == 401

register_page = anonymous.get("/register")
assert register_page.status_code == 200 and b"Email address" in register_page.data
assert anonymous.post("/register", data={
    "name": "Web Student", "email": "web.student@example.edu",
    "password": "secure-web-student-password",
}).status_code == 400
registered = anonymous.post("/register", data={
    "_csrf_token": csrf(register_page),
    "name": "Web Student",
    "email": "Web.Student@Example.edu",
    "password": "secure-web-student-password",
    "role": "ADMIN",
})
assert registered.status_code == 302 and registered.location.endswith("/student")
portal = anonymous.get("/student")
assert portal.status_code == 200 and b"Web Student" in portal.data
with application.app_context():
    web_student = library_db.db.session.query(User).filter_by(
        email="web.student@example.edu").one()
    assert web_student.role == "STUDENT"
    assert web_student.password_hash != "secure-web-student-password"
    web_student_id = web_student.member_id

duplicate_client = application.test_client()
duplicate_page = duplicate_client.get("/register")
duplicate = duplicate_client.post("/register", data={
    "_csrf_token": csrf(duplicate_page),
    "name": "Duplicate Student",
    "email": "WEB.STUDENT@example.edu",
    "password": "secure-web-student-password",
})
assert duplicate.status_code == 409

logout = anonymous.post("/logout", data={"_csrf_token": csrf(portal)})
assert logout.status_code == 302
email_login_page = anonymous.get("/login")
email_login = anonymous.post("/login", data={
    "_csrf_token": csrf(email_login_page),
    "identifier": "WEB.STUDENT@EXAMPLE.EDU",
    "password": "secure-web-student-password",
})
assert email_login.status_code == 302
assert anonymous.get("/student").status_code == 200

student_client = application.test_client()
login_page = student_client.get("/login")
login = student_client.post("/login", data={
    "_csrf_token": csrf(login_page),
    "member_id": student_id,
    "password": "secure-test-student-password",
})
assert login.status_code == 302
portal = student_client.get("/student")
assert portal.status_code == 200 and b"Test Student" in portal.data
token = csrf(portal)
assert "HttpOnly" in login.headers["Set-Cookie"]
assert "SameSite=Lax" in login.headers["Set-Cookie"]
assert student_client.get(f"/members/{student_id}/card").status_code == 200
assert student_client.get("/members/OTHER-STUDENT/card").status_code == 403
assert student_client.get("/dashboard").status_code == 403
assert student_client.get("/reports").status_code == 403
assert student_client.get("/api/analytics").status_code == 403
assert student_client.get("/scan").status_code == 403
assert student_client.post(
    "/api/books", json={}, headers={"X-CSRF-Token": token}).status_code == 403
assert student_client.post(
    "/api/scan/issue", json={}, headers={"X-CSRF-Token": token}).status_code == 403
assert student_client.post(f"/student/reservations/{book_id}").status_code == 400
reserved = student_client.post(
    f"/student/reservations/{book_id}", data={"_csrf_token": token})
assert reserved.status_code == 302
portal = student_client.get("/student")
assert b"Portal Security Book" in portal.data and b"Ready" in portal.data
with application.app_context():
    notification = library_db.db.session.query(Notification).filter_by(
        user_id=library_db.db.session.query(User.id).filter_by(
            member_id=student_id).scalar()).one()
    notification_id = notification.id
assert student_client.post(
    f"/student/notifications/{notification_id}/read",
    data={"_csrf_token": csrf(portal)}).status_code == 302
with application.app_context():
    assert library_db.db.session.get(Notification, notification_id).read_at is not None
student_logout = student_client.post(
    "/logout", data={"_csrf_token": csrf(portal)})
assert student_logout.status_code == 302
assert student_client.get("/student").status_code == 302

admin_client = application.test_client()
login_page = admin_client.get("/login")
admin_login = admin_client.post("/login", data={
    "_csrf_token": csrf(login_page),
    "member_id": "ADMIN-TEST",
    "password": "secure-test-admin-password",
})
assert admin_login.status_code == 302
dashboard = admin_client.get("/dashboard")
assert dashboard.status_code == 200
admin_token = csrf(dashboard)
assert admin_client.get("/scan").status_code == 200
assert admin_client.post("/api/scan/lookup", json={
    "code": "SMARTLIBRARY:STUDENT:STUDENT-TEST",
    "mode": "issue",
}, headers={"X-CSRF-Token": admin_token}).status_code == 200
assert admin_client.get("/members").status_code == 200
assert admin_client.get("/map?q=Portal+Security").status_code == 200

analytics = admin_client.get("/api/analytics")
assert analytics.status_code == 200
assert analytics.json["inventory"]["copies"] == 1
assert len(analytics.json["months"]) == 12
assert analytics.json["most_borrowed"] == []
assert b"monthly-chart" in dashboard.data and b"Most active students" in dashboard.data
assert admin_client.get("/reports").status_code == 200

headers = {"X-CSRF-Token": admin_token}
rack_response = admin_client.post("/api/racks", json={
    "rack_code": "CRUD", "name": "CRUD Rack"}, headers=headers)
assert rack_response.status_code == 201
rack_id = rack_response.json["id"]
shelf_response = admin_client.post(
    f"/api/racks/{rack_id}/shelves", json={"shelf_number": 1}, headers=headers)
assert shelf_response.status_code == 201
shelf_id = shelf_response.json["id"]
category_response = admin_client.post("/api/categories", json={
    "name": "CRUD Category", "rack_id": rack_id}, headers=headers)
assert category_response.status_code == 201
category_id = category_response.json["id"]
book_response = admin_client.post("/api/books", json={
    "title": "CRUD Book", "author": "C. R. U. D.",
    "category_id": category_id, "shelf_id": shelf_id}, headers=headers)
assert book_response.status_code == 201
crud_book_id = book_response.json["book_id"]
assert admin_client.get(f"/api/books/{crud_book_id}").json["title"] == "CRUD Book"
assert admin_client.patch(
    f"/api/books/{crud_book_id}", json={"title": "Updated CRUD Book"},
    headers=headers).status_code == 200
assert admin_client.get(
    f"/api/books/{crud_book_id}").json["title"] == "Updated CRUD Book"
copy_response = admin_client.post(
    f"/api/books/{crud_book_id}/copies", json={"copy_id": "CRUD-COPY"},
    headers=headers)
assert copy_response.status_code == 201
assert admin_client.post(
    f"/api/books/{crud_book_id}/copies", json={"copy_id": "CRUD-COPY"},
    headers=headers).status_code == 409
assert admin_client.get("/copy/CRUD-COPY/qr.svg").mimetype == "image/svg+xml"
assert admin_client.get(f"/members/{student_id}/qr.svg").mimetype == "image/svg+xml"
assert admin_client.delete("/api/copies/CRUD-COPY", headers=headers).status_code == 204
assert admin_client.delete(f"/api/books/{crud_book_id}", headers=headers).status_code == 204
assert admin_client.delete(f"/api/categories/{category_id}", headers=headers).status_code == 204
assert admin_client.delete(f"/api/shelves/{shelf_id}", headers=headers).status_code == 204
assert admin_client.delete(f"/api/racks/{rack_id}", headers=headers).status_code == 204
member_response = admin_client.post("/api/members", json={
    "member_id": "CRUD-MEMBER", "name": "CRUD Member"}, headers=headers)
assert member_response.status_code == 201
assert admin_client.post("/api/members", json={
    "member_id": "CRUD-MEMBER", "name": "Duplicate Member"},
    headers=headers).status_code == 409
assert admin_client.patch(
    "/api/members/CRUD-MEMBER", json={"name": "Updated Member"},
    headers=headers).status_code == 200
assert admin_client.get("/api/members/CRUD-MEMBER").json["name"] == "Updated Member"
assert admin_client.delete("/api/members/CRUD-MEMBER", headers=headers).status_code == 204

copy_scan = admin_client.post("/api/scan/lookup", json={
    "code": "SMARTLIBRARY:COPY:SEC-COPY-1", "mode": "issue",
    "member_id": student_id,
}, headers=headers)
assert copy_scan.status_code == 200 and copy_scan.json["copy"]["available"]
issued = admin_client.post("/api/scan/issue", json={
    "member_id": student_id, "copy_id": "SEC-COPY-1",
}, headers=headers)
assert issued.status_code == 200
assert admin_client.get(f"/api/books/{book_id}").json["available"] == 0
assert admin_client.get("/api/analytics").json["most_borrowed"][0]["title"] == "Portal Security Book"
with application.app_context():
    transaction = library_db.db.session.query(Transaction).one()
    transaction.issue_date = date.today() - timedelta(days=16)
    transaction.due_date = date.today() - timedelta(days=2)
    library_db.db.session.commit()

assert admin_client.get("/api/analytics").json["overdue"]["count"] == 1
assert b"SEC-COPY-1" in admin_client.get("/reports/overdue.csv").data
assert admin_client.get("/reports/overdue.pdf").data.startswith(b"%PDF")
assert b"SEC-COPY-1" in admin_client.get("/reports/inventory.csv").data
assert admin_client.get("/reports/inventory.pdf").data.startswith(b"%PDF")
assert b"SEC-COPY-1" in admin_client.get(
    f"/reports/member/{student_id}/borrowing.csv").data
assert admin_client.get(
    f"/reports/member/{student_id}/borrowing.pdf").data.startswith(b"%PDF")
returned = admin_client.post("/api/scan/return", json={
    "copy_id": "SEC-COPY-1"}, headers=headers)
assert returned.status_code == 200 and returned.json["fine"] == 4
assert admin_client.get(f"/api/books/{book_id}").json["available"] == 1
with application.app_context():
    assert library_db.member_borrowing_report(student_id)["history"][0]["fine"] == 4
assert admin_client.get("/reports/member/NO-SUCH-STUDENT/borrowing.csv").status_code == 404

admin_logout = admin_client.post("/logout", headers=headers)
assert admin_logout.status_code == 302
assert admin_client.get("/dashboard").status_code == 302
with application.app_context():
    library_db.db.session.remove()
    library_db.db.engine.dispose()
    library_db.init_db()
    assert library_db.db.session.query(Book).filter_by(
        title="Portal Security Book").count() == 1
    assert library_db.db.session.query(User).filter_by(
        member_id=student_id).count() == 1
    library_db.db.session.remove()
    library_db.db.engine.dispose()
'''
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(project),
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                result.returncode, 0,
                msg=f"Integration process failed:\n{result.stdout}\n{result.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
