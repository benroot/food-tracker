# Natural Language Calorie Tracker — System Reference

## Purpose
A single-user web app for conversational food and weight logging. Replaces the CSV + chat-based workflow from the original Food & Weight Log project with a proper database and a persistent, always-current interface — while keeping the same casual, natural-language logging experience.

---

## Status
Design phase complete (architecture, schema, model approach, hosting, and deployment approach all agreed). Not yet scaffolded. Next step is to build the initial repo, most likely with Claude Code rather than in-chat, since the remaining work is multi-file implementation and local iteration rather than design decisions.

---

## Architecture

**Message intake**: a chat input on the web page, posting directly to the app's own backend API route.

**Backend**: Python, using **Flask** — chosen explicitly over FastAPI and a stdlib-only approach for simplicity and familiarity. Keep route logic minimal; avoid adding Flask extensions unless a real need shows up (e.g. don't reach for Flask-SQLAlchemy by default — raw `sqlite3` from the stdlib is likely sufficient given the schema size).

**Parsing & calorie estimation**: the backend calls the Claude API with a forced tool call (not free-text JSON) to get guaranteed-structured output. See schema below.

**Claude API access: direct HTTP via `requests`, not the official `anthropic` Python SDK.** This was an explicit decision, not a default. The current `anthropic` SDK (v1.0+, Aug 2026) requires Python 3.10+, which is incompatible with the production server's Python 3.8.20 ceiling (see Deployment Target below). Rather than pin to an old SDK release and risk hitting this same wall again on a future SDK update, the app calls the Messages API directly over HTTPS with `requests` (minimal, and has no Python-version floor anywhere near this app's needs). This is consistent with the project's minimal-stack philosophy — one fewer dependency, and one less thing that can silently break compatibility again later.

**Storage**: SQLite for local development (single file, zero setup), with a **fairly normalized relational schema** — see below. A hosted Postgres (e.g. Supabase or Neon) is a possible future upgrade path but is not the current plan — the confirmed host is cPanel shared hosting with SQLite as a file on disk.

**Frontend**: a chat thread view (log new entries) plus a small persistent summary (today's entries + running total, most recent weight) — deliberately minimal, mirroring the "bland, glanceable" tracker artifact already built in the original project.

---

## Deployment Target

**Hosting**: cPanel-based shared hosting, using cPanel's **"Setup Python App"** tool (Passenger integration). Apache already owns the standard web ports on this host — the app is never a standalone process listening on its own port; Passenger runs it inside Apache's process model. This is what makes shared hosting workable at all here, since opening a custom port isn't available.

**Python version constraint: 3.8.20**, fixed by what the host supports — not a preference, a hard ceiling. This drives two things:
- **Local dev must match**: use `pyenv` to install and pin Python 3.8.20 for this project specifically, without disturbing the system/default Python (3.14) used elsewhere. `pyenv local 3.8.20` in the project root creates a `.python-version` file — commit this to git so the required version is documented and reproducible.
- **Dependency choices must stay 3.8-compatible**: this is the direct reason the Claude API is called via `requests` rather than the official SDK (see Architecture above). Also avoid Python 3.9+/3.10+-only syntax if it ever comes up: no `match`/`case`, no `X | Y` union type hints, no `X | Y` dict-merge operator.
- Worth being aware of (not something in our control): Python 3.8 reached end-of-life in October 2024, so this is a security-patching tradeoff inherent to the host, not something to try to fix locally.

**WSGI entry point**: `passenger_wsgi.py` lives in the project root, alongside the Flask app code, and **is committed to git** (unlike `.env` — it contains no secrets, just the import wiring Passenger needs to find the Flask `app` object under the variable name `application`).

**Environment variables / secrets**: cPanel's "Setup Python App" page has its own environment-variable section — this is the source of truth for `ANTHROPIC_API_KEY` in production, not a `.env` file (some Passenger setups don't reliably auto-load `.env` the way local dev does with `python-dotenv`). Verify directly once deployed rather than assuming parity with local behavior.

**Version control & deploys**:
- Git is used for real version history and rollback safety, regardless of deployment mechanism.
- Deploy process is intentionally manual and lightweight: SSH or cPanel Terminal → `git pull` → click "Restart" on the Python App page. No CI/CD pipeline — deliberately avoided as unnecessary complexity (deploy keys, secrets on a third-party service, a pipeline to debug) for a single-user app deployed occasionally, by hand, on purpose.
- If the cPanel account has SSH/Terminal access, use that directly for `git pull`. If not, check for cPanel's built-in "Git Version Control" tool, which supports cloning/pulling without shell access.

---

## Parsing Tool Schema

Forced tool call pattern — the backend never parses free-text JSON from a prompt; it defines a tool whose schema is the desired shape and forces that tool via `tool_choice`. (Implemented via direct `requests` calls to the Messages API — see Architecture — rather than the `anthropic` SDK's tool-use helpers.)

```json
{
  "name": "log_food_entry",
  "description": "Log one or more food items parsed from the user's message, with estimated calories.",
  "input_schema": {
    "type": "object",
    "properties": {
      "items": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "food": { "type": "string" },
            "quantity": { "type": "string" },
            "estimated_calories": { "type": "integer" },
            "is_estimate": { "type": "boolean" },
            "assumption_note": { "type": "string" }
          },
          "required": ["food", "estimated_calories", "is_estimate"]
        }
      },
      "meal_type": { "type": "string", "enum": ["Breakfast", "Lunch", "Dinner", "Snack", "Dessert", "Drink"] },
      "needs_clarification": { "type": "boolean" },
      "clarification_question": { "type": "string" }
    },
    "required": ["items", "meal_type", "needs_clarification"]
  }
}
```

When `needs_clarification` is true, the backend should ask the follow-up question and hold off writing to the database until resolved — never guess silently on a genuinely ambiguous entry.

**Model choice**: no need for the most expensive tier for straightforward single-item entries; more complex multi-item meals benefit from stronger nutrition reasoning. At single-user volume, cost difference between tiers is negligible either way — simplicity (one model throughout) is a reasonable default unless quality issues show up in practice.

---

## Database Schema (SQLite, normalized)

```sql
-- Reference table: individual food items, reusable across bundles/entries
CREATE TABLE food_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    default_unit TEXT,
    default_calories INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Named, repeatable meals (generalizes the original project's "presets" concept)
CREATE TABLE meal_bundles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Composition of each bundle
CREATE TABLE meal_bundle_items (
    id INTEGER PRIMARY KEY,
    bundle_id INTEGER NOT NULL REFERENCES meal_bundles(id) ON DELETE CASCADE,
    food_item_id INTEGER REFERENCES food_items(id),
    description TEXT NOT NULL,
    quantity TEXT,
    calories INTEGER NOT NULL
);

-- One row per logged meal event
CREATE TABLE log_entries (
    id INTEGER PRIMARY KEY,
    entry_date TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    meal_type TEXT NOT NULL CHECK (meal_type IN ('Breakfast','Lunch','Dinner','Snack','Dessert','Drink')),
    source_bundle_id INTEGER REFERENCES meal_bundles(id),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Actual items logged for that meal — a SNAPSHOT, not a live reference to the bundle
CREATE TABLE log_entry_items (
    id INTEGER PRIMARY KEY,
    log_entry_id INTEGER NOT NULL REFERENCES log_entries(id) ON DELETE CASCADE,
    food_item_id INTEGER REFERENCES food_items(id),
    description TEXT NOT NULL,
    quantity TEXT,
    calories INTEGER NOT NULL,
    is_estimate INTEGER NOT NULL DEFAULT 0,
    assumption_note TEXT
);

CREATE TABLE weight_log (
    id INTEGER PRIMARY KEY,
    entry_date TEXT NOT NULL UNIQUE,
    weight_lbs REAL NOT NULL
);
```

**Deliberate design note**: `log_entry_items` copies calorie values at the time of logging rather than only referencing `meal_bundle_items` by foreign key. This is intentional, not an oversight — if a bundle's recipe is edited later (e.g. less olive oil), past logged entries shouldn't silently change. Reference data (`food_items`, `meal_bundles`) stays normalized and editable; logged history (`log_entries`, `log_entry_items`) stays an immutable snapshot. Event-log tables and reference-catalog tables are allowed to follow different rules on purpose.

---

## Repeatable Meals / "Log it again" Resolution

Claude API calls are stateless — the model has no built-in memory of past conversations or database contents. Resolving something like *"log the salmon rice dish again for dinner"* requires the backend to explicitly retrieve and inject relevant context on each call; it does not happen automatically.

Flow:
1. Before calling Claude, the backend queries `meal_bundles.name` for candidate matches against the message (Python's stdlib `difflib` is sufficient at this scale — no fuzzy-matching library needed yet).
2. The list of known bundle names is included in the prompt context sent to Claude.
3. The tool schema includes a field for Claude to indicate the message appears to reference a known saved meal (rather than re-estimating calories from scratch).
4. The backend resolves that reference against the real `meal_bundles` table:
   - Single clear match → log a new `log_entries` row with `source_bundle_id` set, copying the bundle's current items into `log_entry_items`.
   - Ambiguous or no match → `needs_clarification: true`, with a question naming the close candidates (e.g. *"Did you mean the Salmon rice bowl?"*).
5. **Possible future addition, not required for v1**: after logging an ad hoc multi-item meal that doesn't match an existing bundle, offer to save it as a new bundle (*"Want to save this as a repeatable meal?"*) — turns spontaneous meals into reusable ones without manual setup.

---

## Local Development

- Python **3.8.20**, managed via `pyenv` (`pyenv local 3.8.20`), to match the production constraint exactly rather than developing against a newer version and discovering incompatibilities later.
- `ANTHROPIC_API_KEY` via environment variable / `.env` locally (gitignored, never committed) — production uses cPanel's environment-variable UI instead, not `.env` (see Deployment Target).
- SQLite file for local data — no external services required to develop or test.
- Local dev server + browser at localhost; manually test by sending a food message through the chat UI and confirming the row lands correctly in the SQLite file (inspectable with a tool like DB Browser for SQLite).
- Automated tests never call the live Claude API — see Testing Strategy below.

---

## Testing Strategy

**Framework**: stdlib `unittest` + `unittest.mock` — no `pytest` or other test dependency added, consistent with the minimal-stack philosophy (see Development Style). Revisit only if a real limitation shows up in practice, and flag it explicitly first, per the no-silent-additions rule.

**Location & running**: tests live in `tests/`, one file per module under test (`test_claude_client.py`, `test_app.py`, ...). Run the full suite with:
```
python -m unittest discover -s tests -v
```

**No live API calls in the automated suite, ever.** `claude_client.call_claude` (or the underlying `requests.post`) is mocked with a canned response shaped like the real Messages API tool-use payload, so the suite is fast, free, deterministic, and runnable without `ANTHROPIC_API_KEY` set. A live call against the real endpoint is a manual, occasional smoke test — worth doing after touching request/response handling in `claude_client.py` (e.g. a schema or auth change), but never something the automated suite does on its own. Keep any such manual check to the minimum number of calls needed (usually one) and a short, unambiguous prompt, to keep token cost negligible.

**What to test at each layer**:
- `claude_client.py`: request shape (headers, forced `tool_choice`) and response parsing (`extract_tool_input`, `response_as_message`, `build_user_turn`'s tool_result stitching) — via mocked `requests.post`.
- `app.py` routes: exercised through Flask's test client against a scratch SQLite file (`FOOD_LOG_DB_PATH` env override, cleaned up after the run), with `claude_client.call_claude` mocked so route/DB/session logic is verified independent of the API.

**How this should shape day-to-day development**:
- A new route, or a new branch in an existing route's logic (e.g. Phase 5's bundle-matching resolution), gets a corresponding test before moving on — don't let the suite fall behind the code it's meant to describe.
- Changing the `log_food_entry` tool schema, or the shape of Messages API response handling, means updating the mocked response fixtures in `test_claude_client.py` and `test_app.py` in the same change, not as a follow-up.
- A `schema.sql` change that affects what a route reads or writes gets a test asserting the new columns/rows land correctly — not just a manual DB Browser check.
- A bug fix should come with a regression test reproducing the bug where practical.
- Per the phase-boundary philosophy in Development Phases, run the full suite before calling a phase "done" and moving to the next one — it's the fast, repeatable counterpart to the "actually open it in a browser" check each phase already requires.

---

## Development Style (inferred from the related Food & Weight Log project)

These aren't formally confirmed for this project yet, but are carried over as a reasonable starting point based on how that project's standards were set, and how decisions have been made in conversation so far:

- **Accessibility**: WCAG 2.1 AA minimum on any UI — semantic HTML, labeled form controls, sufficient contrast, visible keyboard focus states, full keyboard operability, respect for `prefers-reduced-motion`.
- **Mobile usability**: mobile and desktop usability should be paramount. A suitable framework for CSS and/or JS may be necessary but should be discussed prior to inclusion.
- **Minimal stack, resist creep**: plain HTML/CSS/JS preferred; add a framework only when there's a real state-management need, not by default. Avoid pulling in libraries "just in case."
- **No silent additions**: before adding any external library, service, font, icon set, or CDN source, name exactly what's being added and why, and get explicit sign-off first. This extends the "no silent imports" rule from the artifact-design requirements to the app itself — e.g., don't add a message-carrier service, a hosted DB, an ORM, or (as happened here) a version-incompatible SDK, without flagging it first.
- **Incremental, discussed decisions over big jumps**: architecture and scope changes (like choosing SQLite over Postgres, or `requests` over the official SDK) get raised and reasoned through explicitly rather than assumed.
- **Prefer the simplest solution that actually fits the scale**: repeatedly, the right call here has been "this is a single-user, low-volume tool — don't over-build it" (SQLite over a hosted DB, one model tier over a routing setup, Flask over FastAPI, manual `git pull` over CI/CD, stdlib `sqlite3`/`difflib`/`requests` over added dependencies where they're sufficient).
- **Data modeling instinct**: reference/catalog data (food items, saved meal bundles) should be normalized and editable; logged history should be treated as an immutable, snapshotted event log even when it duplicates data from a reference table. Don't collapse these into one convenient table just to reduce table count.
- **Casual, conversational tone carries over**: the logging experience itself should stay natural-language and low-friction, consistent with the original project's interaction style.

Worth confirming/adjusting these explicitly once real UI work starts, rather than assuming they transfer perfectly from the other project.

---

## Development Phases

Each phase should leave a genuinely runnable app on the local dev server — something you can actually open in a browser and use for real logging, not just a code milestone. Weight logging comes first specifically as a proof of concept: it's the simplest possible slice of the stack (Flask route → SQLite write → page read-back) with minimal ambiguity to parse, so it's the fastest way to confirm the whole chain actually works end to end in the dev environment before adding the harder parsing logic that food logging needs.

### Phase 1 — Weight logging (proof of concept)
A Flask app with `weight_log` table and a single input using **simple, non-AI parsing** — e.g. a lightweight regex/string parse pulling a number out of something like "my weight today is 233.5" (or just a plain numeric form field, whichever is less code). No Claude API call in this phase; weight entries are unambiguous enough not to need it. A page shows the most recent weight entry.
*Usable for*: confirming Flask + SQLite + the dev server actually work together, with the simplest possible real feature — before spending effort on food parsing's added complexity, and before introducing the Claude API call at all (that arrives in Phase 2). Also a good first checkpoint to confirm the Python 3.8 environment itself is working end to end, before any API-calling code is added.

### Phase 2 — Natural language logging for new (one-off) meals
Introduce the Claude API call for the first time (via direct `requests` calls, per the Architecture decision above): a forced tool call (`log_food_entry`) parses something like "add a banana to breakfast" or "had two eggs and toast around 8am," inferring food, estimated calories, meal type, date (defaulting to today), and time (defaulting to now). Writes to the normalized SQLite schema. A page shows today's entries and running total. No bundle matching yet — every message is treated as a one-off meal.
*Usable for*: the actual day-to-day food logging experience — the core value of the whole project.

### Phase 3 — Direct manual entry (no API call)
A lightweight form alongside the chat input: a food/meal label plus a calorie number, submitted and written straight to `log_entries` / `log_entry_items` without going through the Claude API. Uses `is_estimate = 0` (the value came directly from the user, not a model guess), and the same meal-type and date/time defaulting behavior as Phase 2. No parsing, no ambiguity, no API cost.
*Usable for*: logging anything with an already-known calorie count (e.g. off a packaged food's nutrition label) without spending an API call estimating something that doesn't need estimating, and as a fallback path for logging if the Claude API is ever unavailable or budget-constrained.

### Phase 4 — Direct manipulation of recently logged entries
Add an interface on the food log page for editing or deleting an already-logged entry: correct the label or calorie count, or remove it outright if it was logged in error. Edits write directly to `log_entry_items` (and `log_entries` if the meal type also needs correcting) — a direct database update, not a re-parse through the Claude API. Editing date/time is explicitly out of scope for this phase (see Open Questions): most corrections are "wrong calorie count" or "typo in the food name," not "wrong timestamp," and it's a separable chunk of work with its own accessibility question (native `<input type="date">` + `<input type="time">` is the likely answer whenever it's tackled — see Open Questions).
*Usable for*: fixing the inevitable case where a Claude estimate is off, a label came out mangled, or a message got sent twice by mistake — without needing to open the SQLite file by hand.

### Phase 5 — Meal bundles (manual creation + reuse)
Add `meal_bundles` / `meal_bundle_items` tables and a simple way to save a set of items as a named bundle — a manual "save this as..." action, not yet auto-detected or auto-suggested. Add matching logic so a message like "log the salmon rice dish again" resolves against saved bundle names (the `difflib` + candidate-list-in-prompt approach described above) and either logs it or asks for clarification.
*Usable for*: real repeated-meal logging — the specific need that started this conversation.

### Phase 6 — Unified dashboard
Combine what Phase 1–5 built into one view — today's food entries, running total, and latest weight together — replacing the need to check separate pages.
*Usable for*: full parity with the original CSV-based project, but now backed by a real database and a live web UI instead of file re-uploads.

### Phase 7 — Polish and quality-of-life (optional, only after 1–6 are solid)
Candidates, not commitments: proactive "save as bundle?" prompting after a repeated-looking ad hoc meal, history/trend views, CSV export for backup, accessibility pass against the WCAG 2.1 AA requirement on the real UI (not just the earlier artifact), first-class mobile layout pass. Nothing here should block calling Phase 6 "done" and usable.

### Phase 8 — Visual design / beautification (optional, only after 1–7 are solid)
Candidates, not commitments: a considered color palette and typographic scale beyond the current functional grayscale/system-font baseline, refined spacing and visual hierarchy, small icons for meal types instead of plain text, and light visual branding (the emoji favicon added alongside this phase entry is a first, minimal step in that direction). Any motion or transitions introduced here must still respect `prefers-reduced-motion` per the existing accessibility requirement, and any new font/icon source still needs explicit sign-off first per the no-silent-additions rule. Deliberately kept separate from Phase 7: that phase is about features and data completeness, this one is purely about how the app looks and feels — the app should already be fully functional and accessible before spending effort here.
*Usable for*: nothing new becomes usable that wasn't already — this phase makes daily use of the already-complete app more pleasant, not more capable.

Each phase boundary is a reasonable point to stop, use the app for real, and decide whether the next phase is worth building yet — not a forced march to Phase 8.

---

## Open Questions / Not Yet Decided
- Whether weight logging gets its own parsing tool or reuses a simpler direct-entry path (it's less ambiguous than food, may not need LLM parsing at all)
- Auth approach (likely unnecessary for single-user, but worth an explicit decision rather than default)
- Whether "offer to save as a bundle" gets built in v1 or deferred
- Whether `food_items` gets populated proactively or only grows organically as items are logged
- Whether/when a CSS or JS framework becomes justified for the mobile/desktop usability requirement, and which one — explicitly deferred until there's a concrete need, per the no-silent-additions rule
- Whether/when to extend Phase 4's entry editing to cover date/time (deferred out of Phase 4 itself). If and when it's built, native `<input type="date">` + `<input type="time">` is the recommended approach over a combined `<input type="datetime-local">` or a custom JS picker library: zero added dependency, and both render as accessible native controls on iOS Safari (wheel-style pickers) and desktop browsers (calendar/clock dropdowns), meeting WCAG 2.1 AA by default.
