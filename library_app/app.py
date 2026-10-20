import textwrap
from xml.sax.saxutils import escape

from flask import (Flask, Response, flash, redirect, render_template,
                   request, url_for)

import db
from seed_data import CATEGORIES

app = Flask(__name__)
app.secret_key = "design-studio-library"


@app.template_filter("avail_class")
def avail_class(available):
    return "out" if available == 0 else "low" if available == 1 else "ok"


def conn():
    return db.get_conn()


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
            sections.append({"name": cat["name"], "rack": cat["rack"], "books": items})
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
    members = db.members_with_counts(c)
    c.close()
    return render_template("book.html", b=b, copies=copies, members=members)


@app.route("/scan")
def scan():
    c = conn()
    recent = db.recent_transactions(c, 10)
    c.close()
    return render_template("scan.html", recent=recent)


@app.route("/issue", methods=["POST"])
def issue():
    c = conn()
    ok, msg = db.scan_issue(c, request.form.get("member_id", ""), request.form.get("copy_id", ""))
    c.close()
    flash(msg, "success" if ok else "error")
    return redirect(request.form.get("next") or url_for("scan"))


@app.route("/return", methods=["POST"])
def return_book():
    c = conn()
    ok, msg = db.scan_return(c, request.form.get("copy_id", ""))
    c.close()
    flash(msg, "success" if ok else "error")
    return redirect(request.form.get("next") or url_for("scan"))


@app.route("/dashboard")
def dashboard():
    c = conn()
    data = dict(stats=db.stats(c), overdue=db.overdue_list(c),
                recent=db.recent_transactions(c, 8), cats=db.categories(c),
                most_borrowed=db.most_borrowed(c, 5))
    c.close()
    return render_template("dashboard.html", **data)


@app.route("/map")
def floor_map():
    c = conn()
    cats = db.categories(c)
    c.close()
    return render_template("map.html", categories=cats)


@app.route("/members", methods=["GET", "POST"])
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


@app.route("/labels")
@app.route("/labels/<int:book_id>")
def labels(book_id=None):
    """Printable barcode labels for each copy (stick one on every physical book)."""
    c = conn()
    sql = """SELECT c.copy_id, b.title FROM copies c JOIN books b ON b.book_id=c.book_id"""
    rows = c.execute(sql + (" WHERE b.book_id=?" if book_id else "") + " ORDER BY c.copy_id",
                     (book_id,) if book_id else ()).fetchall()
    c.close()
    return render_template("labels.html", rows=rows)


# ------------------------------------------------------------------ generated cover pages
@app.route("/cover/<int:book_id>.svg")
def cover(book_id):
    c = conn()
    b = db.get_book(c, book_id)
    c.close()
    if not b:
        return "Not found", 404
    _, c1, c2 = CATEGORIES[b["category"]]
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


if __name__ == "__main__":
    db.init_db()
    app.run(debug=True, port=5000)
