-- Gazetteer schema. Raw evidence is append-only; derived tables are
-- rebuilt by gazetteer.build and are likewise never updated in place
-- (new facts create new versions / links, adjudication adds rows).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source (
    source_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('names', 'boundaries', 'notices')),
    description  TEXT,
    reliability  INTEGER NOT NULL DEFAULT 90,
    created_at   TEXT NOT NULL
);

-- One row per file collection attempt, including failures.
CREATE TABLE IF NOT EXISTS ingest_run (
    run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      TEXT NOT NULL REFERENCES source(source_id),
    file_path      TEXT NOT NULL,
    file_sha256    TEXT,
    status         TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    error          TEXT,
    records_loaded INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT NOT NULL,
    finished_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_record (
    record_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id    TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    published_at TEXT NOT NULL,           -- source publication time (wall clock)
    effective_date TEXT,                  -- business date the fact applies to
    kind         TEXT NOT NULL,
    record_key   TEXT NOT NULL,
    payload      TEXT NOT NULL,           -- verbatim JSON, never rewritten
    file_sha256  TEXT NOT NULL,
    run_id       INTEGER NOT NULL REFERENCES ingest_run(run_id),
    ingested_at  TEXT NOT NULL,
    UNIQUE(source_id, batch_id, record_key)
);

CREATE INDEX IF NOT EXISTS idx_raw_effective ON raw_record(effective_date);
CREATE INDEX IF NOT EXISTS idx_raw_kind ON raw_record(kind);

-- Region identity: one stable fictional code, potentially many versions.
CREATE TABLE IF NOT EXISTS region (
    region_id       TEXT PRIMARY KEY,
    first_seen_date TEXT NOT NULL
);

-- Versioned attributes. Intervals are [valid_from, valid_to); valid_to
-- NULL means currently open. A new fact closes the open version and
-- inserts a new one; the old row is never modified beyond closing.
CREATE TABLE IF NOT EXISTS region_version (
    version_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    region_id    TEXT NOT NULL REFERENCES region(region_id),
    name         TEXT NOT NULL,
    parent_id    TEXT,
    valid_from   TEXT NOT NULL,
    valid_to     TEXT,
    dedup_key    TEXT NOT NULL UNIQUE,
    sources_json TEXT NOT NULL,           -- [{source_id, batch_id, record_id, published_at}]
    conflict_json TEXT,                   -- field-level disagreements, kept as evidence
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rv_region ON region_version(region_id);
CREATE INDEX IF NOT EXISTS idx_rv_interval ON region_version(valid_from, valid_to);

-- Evolution relationships. A split/merge group shares group_key so that
-- the release builder can treat parent + children as one atomic unit.
CREATE TABLE IF NOT EXISTS evolution_link (
    link_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    link_type      TEXT NOT NULL CHECK (link_type IN ('split', 'merge', 'rename', 'reuse')),
    from_region_id TEXT,
    to_region_id   TEXT,
    group_key      TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('proposed', 'confirmed', 'adjudicated', 'rejected')),
    source_evidence_json TEXT NOT NULL,
    reason         TEXT,
    decided_by     TEXT,
    decided_at     TEXT,
    UNIQUE(group_key, from_region_id, to_region_id)
);

CREATE INDEX IF NOT EXISTS idx_link_status ON evolution_link(status);
CREATE INDEX IF NOT EXISTS idx_link_effective ON evolution_link(effective_date);

-- Human decisions. Manual adjudication only ever adds rows; collection
-- runs and rebuilds are forbidden to change a decided link/conflict.
CREATE TABLE IF NOT EXISTS adjudication (
    decision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type   TEXT NOT NULL CHECK (subject_type IN
                   ('link', 'field', 'name_reuse', 'boundary_orphan', 'source_gap')),
    link_id        INTEGER REFERENCES evolution_link(link_id),
    region_id      TEXT,
    effective_date TEXT,
    field_name     TEXT,
    decision       TEXT NOT NULL,         -- confirm | reject | override
    override_value TEXT,
    rationale      TEXT NOT NULL,
    decided_by     TEXT NOT NULL,
    decided_at     TEXT NOT NULL
);

-- Work queue for issues the automated pipeline is not allowed to settle.
CREATE TABLE IF NOT EXISTS pending_conflict (
    conflict_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,         -- field_conflict | boundary_orphan | source_gap | name_reuse
    region_id      TEXT,
    effective_date TEXT,
    detail_json    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolution     TEXT,
    decision_id    INTEGER REFERENCES adjudication(decision_id),
    created_at     TEXT NOT NULL,
    UNIQUE(kind, region_id, effective_date, detail_json)
);

-- Immutable published snapshots. Rows are inserted once and never updated.
CREATE TABLE IF NOT EXISTS release (
    release_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    business_date TEXT NOT NULL,
    policy       TEXT NOT NULL CHECK (policy IN ('early', 'aligned')),
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    note         TEXT NOT NULL,
    UNIQUE(business_date, policy)
);

CREATE TABLE IF NOT EXISTS release_item (
    release_id INTEGER NOT NULL REFERENCES release(release_id),
    region_id  TEXT NOT NULL,
    version_id INTEGER NOT NULL REFERENCES region_version(version_id),
    PRIMARY KEY (release_id, region_id)
);
