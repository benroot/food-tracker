"""Generate an AUTH_PASSWORD_HASH value for .env / cPanel's env var UI.

Run locally whenever you want to set or change the login password:

    python generate_password_hash.py

Paste the printed hash into .env (local) and cPanel's Setup Python App
environment-variable UI (production) as AUTH_PASSWORD_HASH, then restart
the app. The plaintext password is never written anywhere.
"""
import getpass

from werkzeug.security import generate_password_hash

password = getpass.getpass("New password: ")
confirm = getpass.getpass("Confirm password: ")

if password != confirm:
    print("Passwords didn't match -- nothing generated.")
else:
    print(generate_password_hash(password))
