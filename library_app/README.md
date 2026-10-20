# Smart Library - scan-based Library Book Manager (Design Studio I)

## Run on your laptop
1. Install Python 3.9+ from python.org (tick "Add to PATH").
2. Open a terminal in this folder and run:
       pip install -r requirements.txt
       python app.py
3. Open http://localhost:5000 in your browser.

The first run creates `library.db` with 100 books, ~380 copies, 8 sample members
and a few issued/overdue books. Delete `library.db` to reset everything.

## Pages
- Catalog (/)         : racks A-J, cover page, author, "x of y available", rack/row, search + category filter
- Book page           : copy-by-copy status, quick issue, return
- Scan desk (/scan)   : type or scan Member ID + Copy ID to issue; Copy ID to return
- Dashboard           : totals, stock per rack, overdue list + fines, recent activity
- Members             : add and list members
- Labels (/labels)    : printable barcodes for every copy (needs internet once for the barcode script)

## Rules (edit in db.py)
LOAN_DAYS = 14, FINE_PER_DAY = 2, MAX_BOOKS = 3

## Files
app.py (routes + cover generator), db.py (database + issue/return logic),
seed_data.py (100 books, racks, members), templates/, static/style.css

## Real cover images (optional)
Covers are generated automatically. To use real photos, put images in static/covers/
and set cover_url, e.g.:
    UPDATE books SET cover_url='/static/covers/1.jpg' WHERE book_id=1;
