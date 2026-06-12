"""SQLite persistence layer — stdlib sqlite3, no ORM."""
import json
import sqlite3
from datetime import datetime, timezone

from fraudscan.config import DB_PATH, ensure_data_dir
from fraudscan.sources.base import Entity

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    uid         TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    source_id   TEXT,
    name        TEXT,
    dba         TEXT,
    entity_type TEXT,
    status      TEXT,
    address     TEXT,
    city        TEXT,
    state       TEXT,
    zip         TEXT,
    county      TEXT,
    lat         REAL,
    lon         REAL,
    amount      REAL,
    source_url  TEXT,
    raw_json    TEXT,
    ingested_at TEXT,
    operator_id TEXT
);
CREATE TABLE IF NOT EXISTS flags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_uid   TEXT NOT NULL,
    source       TEXT,
    rule_id      TEXT,
    severity     REAL,
    title        TEXT,
    explanation  TEXT,
    evidence_json TEXT,
    created_at   TEXT
);
CREATE TABLE IF NOT EXISTS scores (
    entity_uid   TEXT PRIMARY KEY,
    source       TEXT,
    risk_score   REAL,
    flag_count   INTEGER,
    family_count INTEGER,
    top_rule     TEXT,
    updated_at   TEXT
);
CREATE TABLE IF NOT EXISTS ingest_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT,
    record_count INTEGER,
    ran_at       TEXT
);
CREATE TABLE IF NOT EXISTS operators (
    operator_id    TEXT PRIMARY KEY,
    canonical_name TEXT,
    source_count   INTEGER,
    member_count   INTEGER,
    sources_json   TEXT,
    combined_score REAL,
    signals_json   TEXT,
    registry_status TEXT,
    federal_funds   REAL,
    nonprofit_ein   TEXT,
    nonprofit_revenue REAL,
    nonprofit_revoked TEXT,
    audit_findings  INTEGER,
    audit_flagged_amount REAL,
    audit_json      TEXT,
    barred_members  INTEGER,
    sanctioned_members INTEGER,
    contradiction_amount REAL,
    dollars_at_stake REAL,
    strongest_link  TEXT,
    cross_program   INTEGER,
    name_bridged_adverse INTEGER,
    verified_adverse INTEGER,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS operator_members (
    operator_id TEXT,
    entity_uid  TEXT,
    source      TEXT,
    name        TEXT,
    entity_type TEXT,
    status      TEXT,
    risk_score  REAL,
    barred      INTEGER,
    identity    TEXT,
    top_flag    TEXT
);
CREATE INDEX IF NOT EXISTS idx_entities_source ON entities(source);
CREATE INDEX IF NOT EXISTS idx_flags_entity ON flags(entity_uid);
CREATE INDEX IF NOT EXISTS idx_flags_rule ON flags(rule_id);
CREATE INDEX IF NOT EXISTS idx_scores_score ON scores(risk_score);
CREATE TABLE IF NOT EXISTS crosswalk (
    entity_uid TEXT PRIMARY KEY,
    npi        TEXT,
    npi_name   TEXT,
    npi_city   TEXT,
    npi_taxonomy TEXT,
    npi_license  TEXT,
    npi_licenses TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_uid  TEXT,
    program     TEXT,
    period      TEXT,
    amount      REAL,
    source_url  TEXT,
    created_at  TEXT
);
CREATE TABLE IF NOT EXISTS triage (
    entity_uid TEXT PRIMARY KEY,   -- survives re-ingest: keyed by stable uid
    state      TEXT,               -- reviewed | dismissed | escalated | '' (cleared)
    note       TEXT DEFAULT '',
    watched    INTEGER DEFAULT 0,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    run_at     TEXT,               -- one batch per pipeline run (ISO timestamp)
    entity_uid TEXT,
    risk_score REAL,
    flag_count INTEGER,
    funds      REAL,
    status     TEXT,
    PRIMARY KEY (run_at, entity_uid)
);
CREATE INDEX IF NOT EXISTS idx_opmembers ON operator_members(operator_id);
CREATE INDEX IF NOT EXISTS idx_operators_score ON operators(combined_score);
CREATE INDEX IF NOT EXISTS idx_payments_entity ON payments(entity_uid);
"""


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def get_meta(conn, key):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    ensure_data_dir()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn=None):
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    if own:
        conn.close()


def _migrate(conn):
    """Add columns introduced after a DB was first created (CREATE TABLE IF NOT
    EXISTS won't add them to an existing table)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)").fetchall()}
    if "operator_id" not in cols:
        conn.execute("ALTER TABLE entities ADD COLUMN operator_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_operator "
                 "ON entities(operator_id)")
    scols = {r[1] for r in conn.execute("PRAGMA table_info(scores)").fetchall()}
    if scols and "family_count" not in scols:
        conn.execute("ALTER TABLE scores ADD COLUMN family_count INTEGER")
    ocols = {r[1] for r in conn.execute("PRAGMA table_info(operators)").fetchall()}
    for col, typ in (("federal_funds", "REAL"), ("nonprofit_ein", "TEXT"),
                     ("nonprofit_revenue", "REAL"), ("nonprofit_revoked", "TEXT"),
                     ("audit_findings", "INTEGER"),
                     ("audit_flagged_amount", "REAL"), ("audit_json", "TEXT"),
                     ("barred_members", "INTEGER"), ("sanctioned_members", "INTEGER"),
                     ("contradiction_amount", "REAL"), ("dollars_at_stake", "REAL"),
                     ("strongest_link", "TEXT"), ("cross_program", "INTEGER"),
                     ("name_bridged_adverse", "INTEGER"),
                     ("verified_adverse", "INTEGER")):
        if ocols and col not in ocols:
            conn.execute(f"ALTER TABLE operators ADD COLUMN {col} {typ}")
    xcols = {r[1] for r in conn.execute("PRAGMA table_info(crosswalk)").fetchall()}
    for col in ("npi_name", "npi_city", "npi_taxonomy", "npi_license", "npi_licenses"):
        if xcols and col not in xcols:
            conn.execute(f"ALTER TABLE crosswalk ADD COLUMN {col} TEXT")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    mcols = {r[1] for r in conn.execute(
        "PRAGMA table_info(operator_members)").fetchall()}
    for col, typ in (("barred", "INTEGER"), ("identity", "TEXT"), ("top_flag", "TEXT")):
        if mcols and col not in mcols:
            conn.execute(f"ALTER TABLE operator_members ADD COLUMN {col} {typ}")


# ---------- writes ----------

def upsert_entities(conn, entities):
    rows = []
    ts = _now()
    for e in entities:
        rows.append((
            e.uid, e.source, e.source_id, e.name, e.dba, e.entity_type, e.status,
            e.address, e.city, e.state, e.zip, e.county, e.lat, e.lon, e.amount,
            e.source_url, json.dumps(e.raw), ts,
        ))
    conn.executemany(
        """INSERT INTO entities
           (uid, source, source_id, name, dba, entity_type, status, address, city,
            state, zip, county, lat, lon, amount, source_url, raw_json, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(uid) DO UPDATE SET
             name=excluded.name, dba=excluded.dba, entity_type=excluded.entity_type,
             status=excluded.status, address=excluded.address, city=excluded.city,
             state=excluded.state, zip=excluded.zip, county=excluded.county,
             lat=excluded.lat, lon=excluded.lon, amount=excluded.amount,
             source_url=excluded.source_url, raw_json=excluded.raw_json,
             ingested_at=excluded.ingested_at""",
        rows,
    )
    conn.execute(
        "INSERT INTO ingest_runs (source, record_count, ran_at) VALUES (?,?,?)",
        (entities[0].source if entities else "?", len(rows), ts),
    )
    conn.commit()
    return len(rows)


def replace_flags(conn, source, flags):
    """Drop prior flags/scores for a source, then write the fresh set."""
    ts = _now()
    conn.execute("DELETE FROM flags WHERE source=?", (source,))
    conn.executemany(
        """INSERT INTO flags
           (entity_uid, source, rule_id, severity, title, explanation,
            evidence_json, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        [(f.entity_uid, source, f.rule_id, f.severity, f.title, f.explanation,
          json.dumps(f.evidence), ts) for f in flags],
    )
    conn.commit()


def write_scores(conn, source, score_rows):
    ts = _now()
    conn.execute("DELETE FROM scores WHERE source=?", (source,))
    conn.executemany(
        """INSERT INTO scores
           (entity_uid, source, risk_score, flag_count, family_count, top_rule,
            updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        [(r["entity_uid"], source, r["risk_score"], r["flag_count"],
          r.get("family_count", 1), r["top_rule"], ts) for r in score_rows],
    )
    conn.commit()


# ---------- reads ----------

def load_entities(conn, source):
    cur = conn.execute("SELECT * FROM entities WHERE source=?", (source,))
    out = []
    for row in cur.fetchall():
        out.append(Entity(
            source=row["source"], source_id=row["source_id"], name=row["name"] or "",
            dba=row["dba"] or "", entity_type=row["entity_type"] or "",
            status=row["status"] or "", address=row["address"] or "",
            city=row["city"] or "", state=row["state"] or "", zip=row["zip"] or "",
            county=row["county"] or "", lat=row["lat"], lon=row["lon"],
            amount=row["amount"], source_url=row["source_url"] or "",
            raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
        ))
    return out


def entity_count(conn, source=None):
    if source:
        return conn.execute(
            "SELECT COUNT(*) FROM entities WHERE source=?", (source,)
        ).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]


def load_all_entities(conn):
    out = []
    for source in [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM entities").fetchall()]:
        out.extend(load_entities(conn, source))
    return out


def scores_map(conn):
    return {r["entity_uid"]: r["risk_score"]
            for r in conn.execute(
                "SELECT entity_uid, risk_score FROM scores").fetchall()}


def write_payments(conn, rows):
    ts = _now()
    conn.execute("DELETE FROM payments")
    conn.executemany(
        """INSERT INTO payments (entity_uid, program, period, amount, source_url,
             created_at) VALUES (?,?,?,?,?,?)""",
        [(r["entity_uid"], r["program"], r.get("period"), r["amount"],
          r.get("source_url"), ts) for r in rows])
    conn.commit()


def funds_by_entity(conn):
    return {r[0]: r[1] for r in conn.execute(
        "SELECT entity_uid, SUM(amount) FROM payments GROUP BY entity_uid").fetchall()}


def payments_for_entity(conn, uid):
    return [dict(r) for r in conn.execute(
        "SELECT program, period, amount, source_url FROM payments "
        "WHERE entity_uid=? ORDER BY amount DESC", (uid,)).fetchall()]


def total_funds(conn):
    return conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0]


def set_triage(conn, uid, state=None, note=None, watched=None):
    """Upsert a reviewer's triage decision; None leaves that field unchanged."""
    import datetime
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO triage (entity_uid, state, note, watched, updated_at) "
        "VALUES (?, COALESCE(?, ''), COALESCE(?, ''), COALESCE(?, 0), ?) "
        "ON CONFLICT(entity_uid) DO UPDATE SET "
        "state=COALESCE(?, triage.state), note=COALESCE(?, triage.note), "
        "watched=COALESCE(?, triage.watched), updated_at=?",
        (uid, state, note, watched, now, state, note, watched, now))
    conn.commit()


def take_snapshot(conn):
    """Record this run's per-entity vitals so the next run can report what CHANGED."""
    import datetime
    run_at = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO snapshots (run_at, entity_uid, risk_score, flag_count, funds, status) "
        "SELECT ?, s.entity_uid, s.risk_score, s.flag_count, "
        "COALESCE((SELECT SUM(amount) FROM payments p WHERE p.entity_uid=s.entity_uid),0), "
        "e.status FROM scores s JOIN entities e ON e.uid=s.entity_uid", (run_at,))
    # keep the last 6 snapshots
    keep = [r[0] for r in conn.execute(
        "SELECT DISTINCT run_at FROM snapshots ORDER BY run_at DESC LIMIT 6")]
    if keep:
        conn.execute("DELETE FROM snapshots WHERE run_at < ?", (keep[-1],))
    conn.commit()
    return run_at


def upsert_crosswalk(conn, rows):
    """rows: (entity_uid, npi, npi_name, npi_city, npi_taxonomy, npi_license,
    npi_licenses) tuples. Accepts shorter legacy tuples (down to (entity_uid, npi))."""
    norm = [tuple(r) + ("",) * max(0, 7 - len(r)) for r in rows]
    conn.executemany(
        "INSERT INTO crosswalk "
        "(entity_uid, npi, npi_name, npi_city, npi_taxonomy, npi_license, npi_licenses)"
        " VALUES (?,?,?,?,?,?,?) ON CONFLICT(entity_uid) DO UPDATE SET "
        "npi=excluded.npi, npi_name=excluded.npi_name, npi_city=excluded.npi_city, "
        "npi_taxonomy=excluded.npi_taxonomy, npi_license=excluded.npi_license, "
        "npi_licenses=excluded.npi_licenses", norm)
    conn.commit()


def load_crosswalk(conn):
    return {r["entity_uid"]: r["npi"] for r in conn.execute(
        "SELECT entity_uid, npi FROM crosswalk WHERE npi <> ''").fetchall()}


def load_crosswalk_detail(conn):
    """{uid: {npi, name, city, taxonomy, license, licenses}} for identity checks."""
    return {r["entity_uid"]: {"npi": r["npi"], "name": r["npi_name"],
                              "city": r["npi_city"], "taxonomy": r["npi_taxonomy"],
                              "license": r["npi_license"],
                              "licenses": r["npi_licenses"] or ""}
            for r in conn.execute(
                "SELECT entity_uid, npi, npi_name, npi_city, npi_taxonomy, "
                "npi_license, npi_licenses FROM crosswalk WHERE npi <> ''").fetchall()}


def crosswalk_done_uids(conn):
    return {r[0] for r in conn.execute("SELECT entity_uid FROM crosswalk").fetchall()}


def crosswalk_missing_detail(conn):
    """uid -> name for rows resolved to an NPI but lacking the NPPES practice city or
    license number (added later for identity corroboration)."""
    return {r["entity_uid"]: r["name"] for r in conn.execute(
        "SELECT c.entity_uid, e.name FROM crosswalk c JOIN entities e "
        "ON e.uid=c.entity_uid WHERE c.npi <> '' AND "
        "(c.npi_city IS NULL OR c.npi_city='' OR c.npi_license IS NULL "
        " OR c.npi_licenses IS NULL)").fetchall()}


def payments_summary(conn):
    """{uid: {total, program, periods, by_program}} for anomaly + sanction rules.

    by_program carries the true per-program split: {prog: {total, periods}}. The
    top-level `program` is the DOMINANT program (by $) and `periods` the cross-program
    merge — kept for the anomaly rules. (The old version stored whichever program
    appeared first, which mislabeled Part D drug cost as 'Part B' in flag text.)"""
    out = {}
    for r in conn.execute(
            "SELECT entity_uid, program, period, amount FROM payments").fetchall():
        amt = r["amount"] or 0
        d = out.setdefault(r["entity_uid"], {"total": 0.0, "by_program": {}})
        d["total"] += amt
        p = d["by_program"].setdefault(r["program"], {"total": 0.0, "periods": {}})
        p["total"] += amt
        p["periods"][r["period"]] = p["periods"].get(r["period"], 0) + amt
    for d in out.values():
        prog, pd = max(d["by_program"].items(), key=lambda kv: kv[1]["total"])
        d["program"] = prog
        merged = {}
        for pp in d["by_program"].values():
            for k, v in pp["periods"].items():
                merged[k] = merged.get(k, 0) + v
        d["periods"] = merged
    return out


def write_operators(conn, operators):
    ts = _now()
    set_meta(conn, "operators_at", ts)
    conn.execute("DELETE FROM operators")
    conn.execute("DELETE FROM operator_members")
    conn.executemany(
        """INSERT INTO operators (operator_id, canonical_name, source_count,
             member_count, sources_json, combined_score, signals_json,
             registry_status, federal_funds, nonprofit_ein, nonprofit_revenue,
             nonprofit_revoked, audit_findings, audit_flagged_amount, audit_json,
             barred_members, sanctioned_members, contradiction_amount,
             dollars_at_stake, strongest_link, cross_program,
             name_bridged_adverse, verified_adverse, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(o["operator_id"], o["canonical_name"], o["source_count"],
          o["member_count"], json.dumps(o["sources"]), o["combined_score"],
          json.dumps(o["signals"]), o.get("registry_status"),
          o.get("federal_funds"), o.get("nonprofit_ein"),
          o.get("nonprofit_revenue"), o.get("nonprofit_revoked"),
          o.get("audit_findings"),
          o.get("audit_flagged_amount"),
          json.dumps(o["audit_json"]) if o.get("audit_json") else None,
          o.get("barred_members", 0), o.get("sanctioned_members", 0),
          o.get("contradiction_amount", 0.0), o.get("dollars_at_stake", 0.0),
          o.get("strongest_link"), 1 if o.get("cross_program") else 0,
          o.get("name_bridged_adverse", 0), o.get("verified_adverse", 0), ts)
         for o in operators])
    members = []
    for o in operators:
        for m in o["members"]:
            members.append((o["operator_id"], m["uid"], m["source"], m["name"],
                            m["entity_type"], m["status"], m["risk_score"],
                            1 if m.get("barred") else 0, m.get("identity", ""),
                            m.get("top_flag", "")))
    conn.executemany(
        """INSERT INTO operator_members
             (operator_id, entity_uid, source, name, entity_type, status, risk_score,
              barred, identity, top_flag)
           VALUES (?,?,?,?,?,?,?,?,?,?)""", members)
    # Denormalize operator membership onto each entity for fast inline lookup.
    conn.execute("UPDATE entities SET operator_id=NULL WHERE operator_id IS NOT NULL")
    conn.executemany(
        "UPDATE entities SET operator_id=? WHERE uid=?",
        [(o["operator_id"], m["uid"]) for o in operators for m in o["members"]])
    conn.commit()
