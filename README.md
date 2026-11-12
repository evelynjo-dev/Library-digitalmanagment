# Smart Library

## Install and run on Windows

1. Install Python 3.9 or newer.
2. Open PowerShell in `library_app` and install the dependencies:

   ```powershell
   cd library_app
   python -m pip install -r requirements.txt
   ```

3. Configure an administrator account in the same PowerShell window:

   ```powershell
   $env:LIBRARY_SECRET_KEY = (python -c "import secrets; print(secrets.token_urlsafe(48))")
   $env:LIBRARY_ADMIN_ID = "LIB-ADMIN"
   $env:LIBRARY_ADMIN_NAME = "Library Administrator"
   $env:LIBRARY_ADMIN_PASSWORD = python -c "import getpass; print(getpass.getpass('Choose a strong password (12+ characters): '))"
   ```

   Keep the secret key and administrator credentials private; do not commit
   them.

4. Start the server and open http://127.0.0.1:5000:

   ```powershell
   python app.py
   ```

The SQLite database is created in `library_app/library.db`. Existing records
are migrated in place; a new database starts empty and does not insert fake
dashboard data.

Students can register at `/register` with an email address and password, then
sign in using that email. Public registrations are student-only; email
ownership is not verified because no email-delivery service is configured.
Administrators continue to use their configured library ID.

Run automated tests from `library_app` with:

```powershell
python -m unittest discover -s tests -v
```

See [`library_app/README.md`](library_app/README.md) for security setup,
features, API routes, and operational notes.
