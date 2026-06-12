"""Storage tests for operator_id persistence (uses a temp DB)."""
import os
import tempfile
import unittest

from fraudscan import storage
from fraudscan.sources.base import Entity


def _op(op_id, member_uids):
    return {
        "operator_id": op_id, "canonical_name": "X", "sources": ["childcare"],
        "source_count": 1, "member_count": len(member_uids), "combined_score": 50.0,
        "signals": ["s"], "registry_status": None,
        "members": [{"uid": u, "source": "childcare", "name": u,
                     "entity_type": "", "status": "", "risk_score": 0.0}
                    for u in member_uids],
    }


class TestOperatorPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = storage.DB_PATH
        storage.DB_PATH = os.path.join(self.tmp, "t.db")

    def tearDown(self):
        storage.DB_PATH = self._orig

    def test_operator_id_written_and_reset(self):
        conn = storage.connect()
        storage.init_db(conn)
        storage.upsert_entities(conn, [
            Entity(source="childcare", source_id="a", name="A"),
            Entity(source="childcare", source_id="b", name="B"),
            Entity(source="childcare", source_id="c", name="C")])

        storage.write_operators(conn, [_op("op:x", ["childcare:a", "childcare:b"])])
        got = dict(conn.execute("SELECT uid, operator_id FROM entities").fetchall())
        self.assertEqual(got["childcare:a"], "op:x")
        self.assertEqual(got["childcare:b"], "op:x")
        self.assertIsNone(got["childcare:c"])      # not a member

        # rewriting operators clears stale memberships
        storage.write_operators(conn, [_op("op:y", ["childcare:c"])])
        got = dict(conn.execute("SELECT uid, operator_id FROM entities").fetchall())
        self.assertIsNone(got["childcare:a"])
        self.assertEqual(got["childcare:c"], "op:y")
        conn.close()

    def test_migration_adds_column_to_legacy_db(self):
        # simulate an old DB created before the operator_id column existed
        conn = storage.connect()
        conn.executescript(
            "CREATE TABLE entities (uid TEXT PRIMARY KEY, source TEXT, name TEXT);")
        conn.commit()
        storage.init_db(conn)   # should ALTER in the new column, not crash
        cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)").fetchall()}
        self.assertIn("operator_id", cols)
        conn.close()


if __name__ == "__main__":
    unittest.main()
