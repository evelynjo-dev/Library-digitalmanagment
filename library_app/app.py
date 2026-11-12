import csv
from io import BytesIO, StringIO
from datetime import timedelta
from functools import wraps
import os
import secrets
import textwrap
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import qrcode
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from qrcode.image.svg import SvgPathImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle
from werkzeug.security import check_password_hash

import db

app = Flask(__name__)
secret_key = os.environ.get("LIBRARY_SECRET_KEY")
if not secret_key:
    secret_key = secrets.token_urlsafe(48)
    app.logger.warning(
        "LIBRARY_SECRET_KEY is unset; sessions will expire when this process restarts.")
app.config.update(
    SECRET_KEY=secret_key,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("LIBRARY_HTTPS", "").lower() in {"1", "true", "yes"},
)
db.init_app(app)
with app.app_context():
    db.init_db()
    admin_values = (
        os.environ.get("LIBRARY_ADMIN_ID"),
        os.environ.get("LIBRARY_ADMIN_NAME"),
        os.environ.get("LIBRARY_ADMIN_PASSWORD"),
    )
    if any(admin_values):
        if not all(admin_values):
            raise RuntimeError(
                "Set LIBRARY_ADMIN_ID, LIBRARY_ADMIN_NAME, and LIBRARY_ADMIN_PASSWORD together.")
        db.bootstrap_admin(*admin_values)


def _csrf_token():
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)
    return session["_csrf_token"]


app.jinja_env.globals["csrf_token"] = _csrf_token


@app.before_request
def load_request_user():
    user_id = session.get("user_id")
    g.current_user = db.db.session.get(db.User, user_id) if user_id else None
    if user_id and g.current_user is None:
        session.clear()


@app.before_request
def protect_csrf():
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if not session.get("user_id") and request.endpoint != "login":
        return None
    expected = session.get("_csrf_token")
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        if request.path.startswith("/api/"):
            return jsonify(error="CSRF token is missing or invalid."), 400
        abort(400, description="CSRF token is missing or invalid.")


@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self)")
    return response


def _authentication_required():
    if request.path.startswith("/api/"):
        return jsonify(error="Authentication is required."), 401
    return redirect(url_for("login"))


def admin_required(allow_public_read=False):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if allow_public_read and request.method in {"GET", "HEAD", "OPTIONS"}:
                return view(*args, **kwargs)
            if g.current_user is None:
                return _authentication_required()
            if g.current_user.role != "ADMIN":
                if request.path.startswith("/api/"):
                    return jsonify(error="Administrator access is required."), 403
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def student_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.current_user is None:
            return _authentication_required()
        if g.current_user.role != "STUDENT":
            if request.path.startswith("/api/"):
                return jsonify(error="Student access is required."), 403
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.template_filter("avail_class")
def avail_class(available):
    return "out" if available == 0 else "low" if available == 1 else "ok"


def conn():
    return db.db.session


def _safe_local_target(target):
    if not target:
        return None
    parts = urlsplit(target)
    if parts.scheme and parts.scheme not in {"http", "https"}:
        return None
    if parts.netloc and parts.netloc.lower() != request.host.lower():
        return None
    path = parts.path or "/"
    if path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path + (f"?{parts.query}" if parts.query else "")
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")
    member_id = (request.form.get("member_id") or "").strip().upper()
    password = request.form.get("password") or ""
    user = db.db.session.query(db.User).filter_by(member_id=member_id).first()
    if user is None or not user.password_hash or not check_password_hash(
            user.password_hash, password):
        flash("The ID or password is incorrect, or the account has not been activated.", "error")
        return render_template("login.html"), 401
    session.clear()
    session["user_id"] = user.id
    session.permanent = True
    flash(f"Welcome, {user.name}.", "success")
    if user.role == "ADMIN":
        return redirect(url_for("dashboard"))
    return redirect(url_for("student_portal"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("index"))


# ------------------------------------------------------------------ pages
@app.route("/")
def index():
    category = request.args.get("category") or None
    q = (request.args.get("q") or "").strip() or None
    c = conn()
    books = db.catalog(c, category, q)
    cats = db.categories(c)
    c.close()

    sections = []                      # one section per rack, in rack order
    for cat in cats:
        items = [b for b in books if b["category"] == cat["name"]]
        if items:
            sections.append({"name": cat["name"], "rack": cat["rack"],
                             "colors": (cat["color_start"], cat["color_end"]),
                             "books": items})
    return render_template("index.html", sections=sections, categories=cats,
                           category=category, q=q, filtered=bool(category or q),
                           total=len(books))


@app.route("/book/<int:book_id>")
def book(book_id):
    c = conn()
    b = db.get_book(c, book_id)
    if not b:
        c.close()
        return "Book not found", 404
    copies = db.copies_of(c, book_id)
    is_admin = g.current_user is not None and g.current_user.role == "ADMIN"
    members = db.members_with_counts(c) if is_admin else []
    if not is_admin:
        for copy in copies:
            copy["holder"] = None
    c.close()
    return render_template("book.html", b=b, copies=copies, members=members,
                           is_admin=is_admin)


@app.route("/scan")
@admin_required()
def scan():
    c = conn()
    recent = db.recent_transactions(c, 10)
    c.close()
    return render_template("scan.html", recent=recent)


@app.route("/issue", methods=["POST"])
@admin_required()
def issue():
    c = conn()
    ok, msg = db.scan_issue(c, request.form.get("member_id", ""), request.form.get("copy_id", ""))
    c.close()
    flash(msg, "success" if ok else "error")
    return redirect(_safe_local_target(request.form.get("next")) or url_for("scan"))


@app.route("/return", methods=["POST"])
@admin_required()
def return_book():
    c = conn()
    ok, msg = db.scan_return(c, request.form.get("copy_id", ""))
    c.close()
    flash(msg, "success" if ok else "error")
    return redirect(_safe_local_target(request.form.get("next")) or url_for("scan"))


@app.route("/api/scan/lookup", methods=["POST"])
@admin_required()
def api_scan_lookup():
    payload = _api_payload()
    return jsonify(db.lookup_scan(
        payload.get("code"), payload.get("mode"), payload.get("member_id")))


@app.route("/api/scan/issue", methods=["POST"])
@admin_required()
def api_scan_issue():
    payload = _api_payload()
    return jsonify(db.issue_book(payload.get("member_id"), payload.get("copy_id")))


@app.route("/api/scan/return", methods=["POST"])
@admin_required()
def api_scan_return():
    payload = _api_payload()
    return jsonify(db.return_copy(payload.get("copy_id")))


@app.route("/dashboard")
@admin_required()
def dashboard():
    db.generate_due_notifications()
    c = conn()
    analytics = db.analytics_data(c)
    data = dict(stats=analytics["inventory"], analytics=analytics,
                overdue=db.overdue_list(c), recent=db.recent_transactions(c, 8),
                cats=db.categories(c))
    c.close()
    return render_template("dashboard.html", **data)


@app.route("/api/analytics")
@admin_required()
def api_analytics():
    return jsonify(db.analytics_data())


@app.route("/reports")
@admin_required()
def reports():
    return render_template("reports.html", members=db.members_with_counts())


def _csv_response(filename, columns, rows, metadata=None):
    output = StringIO(newline="")
    writer = csv.writer(output)
    for label, value in metadata or []:
        writer.writerow([label, value])
    if metadata:
        writer.writerow([])
    writer.writerow(columns)
    writer.writerows(rows)
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _pdf_response(title, columns, rows, filename, subtitle=None):
    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), rightMargin=12 * mm,
        leftMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "ReportCell", parent=styles["BodyText"], fontName="Helvetica",
        fontSize=7.5, leading=9, wordWrap="CJK",
    )
    header = ParagraphStyle(
        "ReportHeader", parent=body, fontName="Helvetica-Bold",
        textColor=colors.white,
    )
    story = [Paragraph(escape(title), styles["Title"])]
    if subtitle:
        story.append(Paragraph(escape(subtitle), styles["Normal"]))
    table_data = [[Paragraph(escape(str(value)), header) for value in columns]]
    table_data.extend([
        [Paragraph(escape(str(value or "")), body) for value in row]
        for row in rows
    ])
    table = Table(
        table_data, repeatRows=1,
        colWidths=[document.width / len(columns)] * len(columns),
        hAlign="LEFT",
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4f3428")),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d8d0c3")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f6f4ef")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    document.build(story)
    return Response(
        output.getvalue(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/reports/overdue.csv")
@admin_required()
def overdue_csv():
    rows = db.overdue_list()
    columns = ["Member ID", "Member", "Book", "Copy ID", "Due date", "Days late", "Estimated fine (Rs.)"]
    return _csv_response("overdue-books.csv", columns, [
        [row["member_id"], row["name"], row["title"], row["copy_id"],
         row["due_date"], row["late_days"], row["fine"]]
        for row in rows
    ])


@app.route("/reports/overdue.pdf")
@admin_required()
def overdue_pdf():
    rows = db.overdue_list()
    columns = ["Member ID", "Member", "Book", "Copy ID", "Due date", "Days late", "Estimated fine (Rs.)"]
    return _pdf_response("Overdue books report", columns, [
        [row["member_id"], row["name"], row["title"], row["copy_id"],
         row["due_date"], row["late_days"], row["fine"]]
        for row in rows
    ], "overdue-books.pdf")


@app.route("/reports/inventory.csv")
@admin_required()
def inventory_csv():
    rows = db.inventory_report()
    columns = ["Copy ID", "QR payload", "Title", "Author", "Category", "Rack", "Shelf", "Status"]
    return _csv_response("inventory-report.csv", columns, [
        [row["copy_id"], row["qr_code"], row["title"], row["author"],
         row["category"], row["rack"], row["shelf"], row["status"]]
        for row in rows
    ])


@app.route("/reports/inventory.pdf")
@admin_required()
def inventory_pdf():
    rows = db.inventory_report()
    columns = ["Copy ID", "QR payload", "Title", "Author", "Category", "Rack", "Shelf", "Status"]
    return _pdf_response("Inventory report", columns, [
        [row["copy_id"], row["qr_code"], row["title"], row["author"],
         row["category"], row["rack"], row["shelf"], row["status"]]
        for row in rows
    ], "inventory-report.pdf")


@app.route("/reports/member/<member_id>/borrowing.csv")
@admin_required()
def member_borrowing_csv(member_id):
    report = db.member_borrowing_report(member_id)
    if report is None:
        abort(404)
    columns = ["Book", "Copy ID", "Issue date", "Due date", "Return date", "Status", "Fine (Rs.)"]
    return _csv_response(
        "member-borrowing-report.csv", columns,
        [[row["title"], row["copy_id"], row["issue_date"], row["due_date"],
          row["return_date"], row["status"], row["fine"]] for row in report["history"]],
        metadata=[("Member ID", report["member_id"]), ("Member name", report["name"])],
    )


@app.route("/reports/member/<member_id>/borrowing.pdf")
@admin_required()
def member_borrowing_pdf(member_id):
    report = db.member_borrowing_report(member_id)
    if report is None:
        abort(404)
    columns = ["Book", "Copy ID", "Issue date", "Due date", "Return date", "Status", "Fine (Rs.)"]
    return _pdf_response(
        f"Borrowing report: {report['name']}", columns,
        [[row["title"], row["copy_id"], row["issue_date"], row["due_date"],
          row["return_date"], row["status"], row["fine"]] for row in report["history"]],
        "member-borrowing-report.pdf", subtitle=f"Member ID: {report['member_id']}",
    )


@app.route("/map")
def floor_map():
    query = (request.args.get("q") or "").strip()
    racks = db.floor_map_data(query)
    matching_books = sum(
        book["matches"]
        for rack in racks for shelf in rack["shelves"] for book in shelf["books"])
    return render_template(
        "map.html", racks=racks, q=query, matching_books=matching_books)


@app.route("/members", methods=["GET", "POST"])
@admin_required()
def members():
    c = conn()
    if request.method == "POST":
        ok, msg = db.add_member(c, request.form.get("member_id", ""),
                                request.form.get("name", ""), request.form.get("phone", ""))
        flash(msg, "success" if ok else "error")
        c.close()
        return redirect(url_for("members"))
    rows = db.members_with_counts(c)
    c.close()
    return render_template("members.html", members=rows, max_books=db.MAX_BOOKS)


@app.route("/members/<member_id>/password", methods=["POST"])
@admin_required()
def member_password(member_id):
    db.set_member_password(member_id, request.form.get("password"))
    flash(f"Password updated for {member_id.upper()}.", "success")
    return redirect(url_for("members"))


@app.route("/members/<member_id>/card")
def member_card(member_id):
    if g.current_user is None:
        return _authentication_required()
    if g.current_user.role != "ADMIN" and (
            g.current_user.role != "STUDENT"
            or g.current_user.member_id != member_id.upper()):
        abort(403)
    return render_template("member_card.html", member=db.member_card(member_id))


@app.route("/student")
@student_required
def student_portal():
    db.generate_due_notifications(g.current_user.member_id)
    return render_template(
        "student_portal.html",
        portal=db.student_portal_data(g.current_user.member_id),
        member_id=g.current_user.member_id,
        fine_per_day=db.FINE_PER_DAY,
    )


@app.route("/student/reservations/<int:book_id>", methods=["POST"])
@student_required
def reserve_book(book_id):
    reservation = db.create_reservation(g.current_user.member_id, book_id)
    flash(
        f"Reservation #{reservation['id']} is {reservation['status'].lower()}. "
        f"Queue position: {reservation['queue_position'] or '—'}.",
        "success",
    )
    return redirect(url_for("student_portal"))


@app.route("/student/reservations/<int:reservation_id>/cancel", methods=["POST"])
@student_required
def cancel_student_reservation(reservation_id):
    db.cancel_reservation(reservation_id, g.current_user.member_id)
    flash("Reservation cancelled.", "success")
    return redirect(url_for("student_portal"))


@app.route("/student/notifications/<int:notification_id>/read", methods=["POST"])
@student_required
def mark_student_notification_read(notification_id):
    db.mark_notification_read(notification_id, g.current_user.member_id)
    return redirect(url_for("student_portal"))


@app.route("/labels")
@app.route("/labels/<int:book_id>")
@admin_required()
def labels(book_id=None):
    """Printable barcode labels for each copy (stick one on every physical book)."""
    c = conn()
    rows = db.labels(c, book_id)
    c.close()
    return render_template("labels.html", rows=rows)


@app.route("/copy/<copy_id>/qr.svg")
def copy_qr(copy_id):
    value = db.copy_qr_value(copy_id)
    return _qr_svg(value)


@app.route("/members/<member_id>/qr.svg")
def member_qr(member_id):
    if g.current_user is None:
        return _authentication_required()
    if g.current_user.role != "ADMIN" and (
            g.current_user.role != "STUDENT"
            or g.current_user.member_id != member_id.upper()):
        abort(403)
    value = db.member_card(member_id)["qr_code"]
    return _qr_svg(value)


def _qr_svg(value):
    code = qrcode.QRCode(box_size=5, border=2)
    code.add_data(value)
    code.make(fit=True)
    image = code.make_image(image_factory=SvgPathImage)
    output = BytesIO()
    image.save(output)
    return Response(output.getvalue(), mimetype="image/svg+xml")


def _api_payload():
    if request.is_json:
        payload = request.get_json()
        if not isinstance(payload, dict):
            raise db.ValidationError("The request body must be a JSON object.")
        return payload
    return request.form.to_dict()


def _json_error(error, status):
    if request.path.startswith("/api/"):
        return jsonify(error=str(error)), status
    flash(str(error), "error")
    return redirect(_safe_local_target(request.referrer) or url_for("index"))


@app.errorhandler(db.NotFoundError)
def handle_not_found(error):
    return _json_error(error, 404)


@app.errorhandler(db.ConflictError)
def handle_conflict(error):
    return _json_error(error, 409)


@app.errorhandler(db.ValidationError)
def handle_validation(error):
    return _json_error(error, 400)


@app.errorhandler(db.DatabaseError)
def handle_database_error(error):
    app.logger.error("Database operation failed: %s", error)
    if request.path.startswith("/api/"):
        return jsonify(error="Database operation failed."), 500
    flash("Database operation failed. Please try again or contact the librarian.", "error")
    return redirect(request.referrer or url_for("index")), 500


@app.route("/api/books", methods=["GET", "POST"])
@admin_required(allow_public_read=True)
def api_books():
    if request.method == "GET":
        return jsonify(db.catalog(category=request.args.get("category"),
                                  q=request.args.get("q")))
    book = db.create_book(_api_payload())
    return jsonify(db.serialize_book(book)), 201


@app.route("/api/books/<int:book_id>", methods=["GET", "PUT", "PATCH", "DELETE"])
@admin_required(allow_public_read=True)
def api_book(book_id):
    if request.method == "GET":
        book = db.get_book(None, book_id)
        if book is None:
            raise db.NotFoundError("Book was not found.")
        return jsonify(book)
    if request.method == "DELETE":
        db.delete_book(book_id)
        return "", 204
    return jsonify(db.serialize_book(db.update_book(book_id, _api_payload())))


@app.route("/api/books/<int:book_id>/copies", methods=["GET", "POST"])
@admin_required(allow_public_read=True)
def api_book_copies(book_id):
    if request.method == "GET":
        copies = db.copies_of(None, book_id)
        if g.current_user is None or g.current_user.role != "ADMIN":
            for copy in copies:
                copy["holder"] = None
        return jsonify(copies)
    copy = db.create_copy(book_id, _api_payload())
    return jsonify(db.copy_details(copy.copy_id)), 201


@app.route("/api/copies/<copy_id>", methods=["GET", "DELETE"])
@admin_required(allow_public_read=True)
def api_copy(copy_id):
    if request.method == "GET":
        return jsonify(db.copy_details(copy_id))
    db.delete_copy(copy_id)
    return "", 204


@app.route("/api/members", methods=["GET", "POST"])
@admin_required()
def api_members():
    if request.method == "GET":
        return jsonify(db.list_members())
    member = db.create_member(_api_payload())
    return jsonify(db.serialize_member(member)), 201


@app.route("/api/members/<member_id>", methods=["GET", "PUT", "PATCH", "DELETE"])
@admin_required()
def api_member(member_id):
    member = next((row for row in db.list_members()
                   if row["member_id"] == member_id.upper()), None)
    if request.method == "GET":
        if member is None:
            raise db.NotFoundError("Member was not found.")
        return jsonify(member)
    if request.method == "DELETE":
        db.delete_member(member_id)
        return "", 204
    return jsonify(db.serialize_member(db.update_member(member_id, _api_payload())))


@app.route("/api/members/<member_id>/password", methods=["POST"])
@admin_required()
def api_member_password(member_id):
    db.set_member_password(member_id, _api_payload().get("password"))
    return jsonify(message="Student password updated.")


@app.route("/api/categories", methods=["GET", "POST"])
@admin_required(allow_public_read=True)
def api_categories():
    if request.method == "GET":
        return jsonify(db.list_categories())
    category = db.create_category(_api_payload())
    return jsonify(db.serialize_category(category)), 201


@app.route("/api/categories/<int:category_id>", methods=["GET", "PUT", "PATCH", "DELETE"])
@admin_required(allow_public_read=True)
def api_category(category_id):
    if request.method == "GET":
        category = next((item for item in db.list_categories()
                         if item["id"] == category_id), None)
        if category is None:
            raise db.NotFoundError("Category was not found.")
        return jsonify(category)
    if request.method == "DELETE":
        db.delete_category(category_id)
        return "", 204
    return jsonify(db.serialize_category(
        db.update_category(category_id, _api_payload())))


@app.route("/api/racks", methods=["GET", "POST"])
@admin_required(allow_public_read=True)
def api_racks():
    if request.method == "GET":
        return jsonify(db.list_racks())
    rack = db.create_rack(_api_payload())
    return jsonify(db.serialize_rack(rack)), 201


@app.route("/api/racks/<int:rack_id>", methods=["GET", "PUT", "PATCH", "DELETE"])
@admin_required(allow_public_read=True)
def api_rack(rack_id):
    if request.method == "GET":
        rack = next((item for item in db.list_racks() if item["id"] == rack_id), None)
        if rack is None:
            raise db.NotFoundError("Rack was not found.")
        return jsonify(rack)
    if request.method == "DELETE":
        db.delete_rack(rack_id)
        return "", 204
    return jsonify(db.serialize_rack(db.update_rack(rack_id, _api_payload())))


@app.route("/api/racks/<int:rack_id>/shelves", methods=["GET", "POST"])
@admin_required(allow_public_read=True)
def api_rack_shelves(rack_id):
    if request.method == "GET":
        return jsonify(db.shelves_for_rack(rack_id))
    return jsonify(db.serialize_shelf(
        db.create_shelf(rack_id, _api_payload()))), 201


@app.route("/api/shelves/<int:shelf_id>", methods=["GET", "PUT", "PATCH", "DELETE"])
@admin_required(allow_public_read=True)
def api_shelf(shelf_id):
    if request.method == "GET":
        return jsonify(db.get_shelf(shelf_id))
    if request.method == "DELETE":
        db.delete_shelf(shelf_id)
        return "", 204
    return jsonify(db.serialize_shelf(db.update_shelf(shelf_id, _api_payload())))


# ------------------------------------------------------------------ generated cover pages
@app.route("/cover/<int:book_id>.svg")
def cover(book_id):
    c = conn()
    b = db.get_book(c, book_id)
    c.close()
    if not b:
        return "Not found", 404
    c1, c2 = b["color_start"], b["color_end"]
    lines = textwrap.wrap(b["title"], 15)[:5]
    title_svg = "".join(
        f'<text x="100" y="{112 + i * 26}" text-anchor="middle" font-size="21" '
        f'font-weight="700" fill="#fff">{escape(line)}</text>' for i, line in enumerate(lines))
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>
<rect width="200" height="300" rx="6" fill="url(#g)"/>
<rect width="14" height="300" rx="6" fill="#000" opacity=".18"/>
<circle cx="170" cy="40" r="60" fill="#fff" opacity=".10"/>
<circle cx="30" cy="285" r="70" fill="#fff" opacity=".08"/>
<text x="107" y="34" text-anchor="middle" font-size="10" letter-spacing="2" fill="#fff" opacity=".85">{escape(b['category'].upper())}</text>
<line x1="60" y1="46" x2="154" y2="46" stroke="#fff" opacity=".6"/>
{title_svg}
<text x="107" y="274" text-anchor="middle" font-size="12" fill="#fff" opacity=".95">{escape(b['author'])}</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.cli.command("generate-due-notifications")
def generate_due_notifications_command():
    with app.app_context():
        created = db.generate_due_notifications()
    print(f"Created {created} due-date notification(s).")


if __name__ == "__main__":
    app.run(host="127.0.0.1", debug=True, port=5000)
