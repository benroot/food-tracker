CREATE TABLE IF NOT EXISTS weight_log (
    id INTEGER PRIMARY KEY,
    entry_date TEXT NOT NULL UNIQUE,
    weight_lbs REAL NOT NULL
);
