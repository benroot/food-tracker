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

Not required yet — Phase 1 (weight logging) has no external API calls. Starting in Phase 2, an `ANTHROPIC_API_KEY` will be needed for food-entry parsing; it will go in a gitignored `.env` file.
