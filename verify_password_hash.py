"""Diagnostic-only: check whether a password actually matches a generated
AUTH_PASSWORD_HASH value, entirely locally -- no server, no cPanel, no HTTP.

Useful when the login gate rejects a password you're sure is right: paste in
the exact base64 value you put in cPanel (or that generate_password_hash.py
just printed) and the password you're typing into the browser prompt, and see
whether Werkzeug itself considers them a match -- the same base64-decode +
check_password_hash steps app.py performs on every request.

    python verify_password_hash.py
"""
import base64
import getpass

from werkzeug.security import check_password_hash

encoded = input("AUTH_PASSWORD_HASH value to test (the base64 string): ").strip()
password = getpass.getpass("Password to test: ")

try:
    hash_value = base64.urlsafe_b64decode(encoded).decode()
except Exception as exc:
    print(f"Couldn't base64-decode that value -- {exc}")
else:
    print("decoded hash:", hash_value)
    if check_password_hash(hash_value, password):
        print("MATCH -- this password is correct for this hash.")
    else:
        print("NO MATCH -- this password does not verify against this hash.")
