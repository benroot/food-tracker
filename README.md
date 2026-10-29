# Natural Language Calorie Tracker

See [CLAUDE.md](CLAUDE.md) for architecture, schema, and phase plan.

## Setup

```
python -m venv venv
```

Activate the venv:

- PowerShell: `venv\Scripts\Activate.ps1`
- Git Bash: `source venv/Scripts/activate`

Install dependencies:

```
pip install -r requirements.txt
```

## Run

```
python app.py
```

Open http://127.0.0.1:5000 in a browser.

The SQLite database (`food_log.db`) and its tables are created automatically on first run.

## Environment variables

Create a `.env` file (gitignored, copy from `.env.example`) with:

```
ANTHROPIC_API_KEY=your-key-here
```

Used by the chat-based food logging route (`/`) to parse food entries via the Claude API. Weight logging (`/weight`) doesn't need it.

`AUTH_PASSWORD_HASH` + `SECRET_KEY` (optional locally, **required in production**) turn on the login page. Generate the first with `python generate_password_hash.py` and the second with `python -c "import secrets; print(secrets.token_hex(32))"`. If neither is set, the login gate is off. Setting only the hash makes the app refuse to start. Over plain `http://localhost`, also set `ALLOW_INSECURE_COOKIES=1`, or the browser won't keep the login cookie. See CLAUDE.md → Authentication.

`FOOD_LOG_DB_PATH` (optional) overrides the SQLite file path — useful for pointing at a scratch database while testing instead of the real `food_log.db`.

## Tests

```
python -m unittest discover -s tests -v
```

Mocks the Claude API (`requests.post`) — no `ANTHROPIC_API_KEY` needed and no live calls made.
