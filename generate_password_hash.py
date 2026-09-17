"""Generate an AUTH_PASSWORD_HASH value for .env / cPanel's env var UI.

Run locally whenever you want to set or change the login password:

    python generate_password_hash.py

Paste the printed value into .env (local) and cPanel's Setup Python App
environment-variable UI (production) as AUTH_PASSWORD_HASH, then restart
the app. The plaintext password is never written anywhere.

The printed value is the real password hash, base64-encoded -- not the raw
scrypt:... string. cPanel's Setup Python App env-var pipeline was found to
corrupt the raw form: something in it shell-interpolates the $salt$hash
portion as variable references, silently dropping whatever's between the $
delimiters. Base64's alphabet has no $, so there's nothing left to mangle.
app.py decodes it back on read, so nothing downstream needs to know about
this -- it's purely a storage-transport workaround for cPanel's env vars.

Uses pbkdf2:sha256, not Werkzeug's scrypt default -- scrypt needs OpenSSL
1.1+ built with scrypt support, which production's Python 3.8.20 build on
the cPanel host doesn't have (AttributeError: module 'hashlib' has no
attribute 'scrypt', surfaced at verification time, i.e. on every login
attempt). pbkdf2_hmac has no such dependency -- pure HMAC, always available
in hashlib -- so this is a hosting-compatibility requirement, not a
preference. Don't switch this back to scrypt without confirming production's
Python build actually supports it.
"""
import base64
import getpass

from werkzeug.security import generate_password_hash

password = getpass.getpass("New password: ")
confirm = getpass.getpass("Confirm password: ")

if password != confirm:
    print("Passwords didn't match -- nothing generated.")
else:
    hash_value = generate_password_hash(password, method="pbkdf2:sha256")
    print(base64.urlsafe_b64encode(hash_value.encode()).decode())
