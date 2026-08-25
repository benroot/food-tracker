# Natural Language Calorie Tracker — System Reference

## Purpose
A single-user web app for conversational food and weight logging. Replaces the CSV + chat-based workflow from the original Food & Weight Log project with a proper database and a persistent, always-current interface — while keeping the same casual, natural-language logging experience.

---

## Status
Design phase complete (architecture, schema, model approach agreed). Not yet scaffolded. Next step is to build the initial repo, most likely with Claude Code rather than in-chat, since the remaining work is multi-file implementation and local iteration rather than design decisions.

---

## Architecture

**Message intake**: a chat input on the web page, posting directly to the app's own backend API route.

**Backend**: Python, using **Flask** — chosen explicitly over FastAPI and a stdlib-only approach for simplicity and familiarity. Keep route logic minimal; avoid adding Flask extensions unless a real need shows up (e.g. don't reach for Flask-SQLAlchemy by default — raw `sqlite3` from the stdlib is likely sufficient given the schema size).

**Parsing & calorie estimation**: the backend calls the Claude API with a forced tool call (not free-text JSON) to get guaranteed-structured output. See schema below.

**Storage**: SQLite for local development (single file, zero setup), with a **fairly normalized relational schema** — see below. A hosted Postgres (e.g. Supabase or Neon) is the likely upgrade path if/when this moves beyond local dev — not needed before then.

**Frontend**: a chat thread view (log new entries) plus a small persistent summary (today's entries + running total, most recent weight) — deliberately minimal, mirroring the "bland, glanceable" tracker artifact already built in the original project.

---

## Parsing Tool Schema

Forced tool call pattern — the backend never parses free-text JSON from a prompt; it defines a tool whose schema is the desired shape and forces that tool via `tool_choice`.

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

- `ANTHROPIC_API_KEY` via environment variable / `.env` (gitignored, never committed)
- SQLite file for local data — no external services required to develop or test
- Local dev server + browser at localhost; manually test by sending a food message through the chat UI and confirming the row lands correctly in the SQLite file (inspectable with a tool like DB Browser for SQLite)
- Basic tests should mock the Anthropic API response rather than hitting the live API on every test run

---

## Development Style (inferred from the related Food & Weight Log project)

These aren't formally confirmed for this project yet, but are carried over as a reasonable starting point based on how that project's standards were set, and how decisions have been made in conversation so far:

- **Accessibility**: WCAG 2.1 AA minimum on any UI — semantic HTML, labeled form controls, sufficient contrast, visible keyboard focus states, full keyboard operability, respect for `prefers-reduced-motion`.
- **Mobile usability**: Mobile and Desktop usability should be paramount. A suitable framework for CSS and/or JS may be necessary but should be discussed prior to inclusion.
- **Minimal stack, resist creep**: plain HTML/CSS/JS preferred; add a framework only when there's a real state-management need, not by default. Avoid pulling in libraries "just in case."
- **No silent additions**: before adding any external library, service, font, icon set, or CDN source, name exactly what's being added and why, and get explicit sign-off first. This extends the "no silent imports" rule from the artifact-design requirements to the app itself — e.g., don't add a message-carrier service, a hosted DB, or an ORM without flagging it first.
- **Incremental, discussed decisions over big jumps**: architecture and scope changes (like choosing SQLite over Postgres) get raised and reasoned through explicitly rather than assumed.
- **Prefer the simplest solution that actually fits the scale**: repeatedly, the right call here has been "this is a single-user, low-volume tool — don't over-build it" (SQLite over a hosted DB, one model tier over a routing setup, Flask over FastAPI, stdlib `sqlite3`/`difflib` over added dependencies where they're sufficient).
- **Data modeling instinct**: reference/catalog data (food items, saved meal bundles) should be normalized and editable; logged history should be treated as an immutable, snapshotted event log even when it duplicates data from a reference table. Don't collapse these into one convenient table just to reduce table count.
- **Casual, conversational tone carries over**: the logging experience itself should stay natural-language and low-friction, consistent with the original project's interaction style.

Worth confirming/adjusting these explicitly once real UI work starts, rather than assuming they transfer perfectly from the other project.

---

## Development Phases

Each phase should leave a genuinely runnable app on the local dev server — something you can actually open in a browser and use for real logging, not just a code milestone. Weight logging comes first specifically as a proof of concept: it's the simplest possible slice of the stack (Flask route → SQLite write → page read-back) with minimal ambiguity to parse, so it's the fastest way to confirm the whole chain actually works end to end in the dev environment before adding the harder parsing logic that food logging needs.

### Phase 1 — Weight logging (proof of concept)
A Flask app with `weight_log` table and a single input using **simple, non-AI parsing** — e.g. a lightweight regex/string parse pulling a number out of something like "my weight today is 233.5" (or just a plain numeric form field, whichever is less code). No Claude API call in this phase; weight entries are unambiguous enough not to need it. A page shows the most recent weight entry.
*Usable for*: confirming Flask + SQLite + the dev server actually work together, with the simplest possible real feature — before spending effort on food parsing's added complexity, and before introducing the Claude API call at all (that arrives in Phase 2).

### Phase 2 — Natural language logging for new (one-off) meals
Introduce the Claude API call for the first time: a forced tool call (`log_food_entry`) parses something like "add a banana to breakfast" or "had two eggs and toast around 8am," inferring food, estimated calories, meal type, date (defaulting to today), and time (defaulting to now). Writes to the normalized SQLite schema. A page shows today's entries and running total. No bundle matching yet — every message is treated as a one-off meal.
*Usable for*: the actual day-to-day food logging experience — the core value of the whole project.

### Phase 3 — Meal bundles (manual creation + reuse)
Add `meal_bundles` / `meal_bundle_items` tables and a simple way to save a set of items as a named bundle — a manual "save this as..." action, not yet auto-detected or auto-suggested. Add matching logic so a message like "log the salmon rice dish again" resolves against saved bundle names (the `difflib` + candidate-list-in-prompt approach described above) and either logs it or asks for clarification.
*Usable for*: real repeated-meal logging — the specific need that started this conversation.

### Phase 4 — Unified dashboard
Combine what Phase 1–3 built into one view — today's food entries, running total, and latest weight together — replacing the need to check separate pages.
*Usable for*: full parity with the original CSV-based project, but now backed by a real database and a live web UI instead of file re-uploads.

### Phase 5 — Polish and quality-of-life (optional, only after 1–4 are solid)
Candidates, not commitments: proactive "save as bundle?" prompting after a repeated-looking ad hoc meal, history/trend views, CSV export for backup, accessibility pass against the WCAG 2.1 AA requirement on the real UI (not just the earlier artifact). Nothing here should block calling Phase 4 "done" and usable.

Each phase boundary is a reasonable point to stop, use the app for real, and decide whether the next phase is worth building yet — not a forced march to Phase 5.
- Exact hosting choice when moving past local dev
- Whether weight logging gets its own parsing tool or reuses a simpler direct-entry path (it's less ambiguous than food, may not need LLM parsing at all)
- Auth approach (likely unnecessary for single-user, but worth an explicit decision rather than default)
- Whether "offer to save as a bundle" gets built in v1 or deferred
- Whether `food_items` gets populated proactively or only grows organically as items are logged
