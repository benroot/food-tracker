CREATE TABLE IF NOT EXISTS weight_log (
    id INTEGER PRIMARY KEY,
    entry_date TEXT NOT NULL UNIQUE,
    weight_lbs REAL NOT NULL
);

-- Reference table: individual food items, reusable across bundles/entries.
-- Not yet populated proactively in Phase 2 -- log_entry_items.food_item_id
-- stays NULL until Phase 3 introduces bundle/catalog matching.
CREATE TABLE IF NOT EXISTS food_items (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    default_unit TEXT,
    default_calories INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per logged meal event
CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY,
    entry_date TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    meal_type TEXT NOT NULL CHECK (meal_type IN ('Breakfast','Lunch','Dinner','Snack','Dessert','Drink')),
    source_bundle_id INTEGER REFERENCES meal_bundles(id),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Actual items logged for that meal -- a snapshot, not a live reference
CREATE TABLE IF NOT EXISTS log_entry_items (
    id INTEGER PRIMARY KEY,
    log_entry_id INTEGER NOT NULL REFERENCES log_entries(id) ON DELETE CASCADE,
    food_item_id INTEGER REFERENCES food_items(id),
    description TEXT NOT NULL,
    quantity TEXT,
    calories INTEGER NOT NULL,
    is_estimate INTEGER NOT NULL DEFAULT 0,
    assumption_note TEXT
);

-- One row per logged exercise activity. Deliberately flat (unlike log_entries/
-- log_entry_items) since one exercise entry is naturally one activity, with no
-- multi-item-meal-style need for a child table. See Exercise Logging Phases.
CREATE TABLE IF NOT EXISTS exercise_log (
    id INTEGER PRIMARY KEY,
    entry_date TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    activity TEXT NOT NULL,
    calories_burned INTEGER NOT NULL,
    is_estimate INTEGER NOT NULL DEFAULT 0,
    assumption_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
