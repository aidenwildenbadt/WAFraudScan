"""Vercel Python Function entrypoint."""
import gzip
import os
import shutil

os.environ.setdefault("FRAUDSCAN_DATA_DIR", "/tmp/fraudscan-data")

from fraudscan.config import DB_PATH, ROOT  # noqa: E402
from fraudscan.web.server import Handler  # noqa: E402
from fraudscan import storage  # noqa: E402

SEED_DB_GZ = os.path.join(ROOT, "deploy", "fraudscan-seed.db.gz")
_SEEDED = False


def _ensure_seeded_db():
    """Hydrate Vercel's ephemeral /tmp database from the packaged seed once."""
    global _SEEDED
    if _SEEDED and os.path.exists(DB_PATH):
        return
    if os.path.exists(DB_PATH) or not os.path.exists(SEED_DB_GZ):
        _SEEDED = True
        return
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    tmp_path = DB_PATH + ".tmp"
    with gzip.open(SEED_DB_GZ, "rb") as src, open(tmp_path, "wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp_path, DB_PATH)
    _SEEDED = True


class handler(Handler):  # noqa: N801
    def do_GET(self):
        _ensure_seeded_db()
        storage.init_db()
        super().do_GET()

    def do_POST(self):
        _ensure_seeded_db()
        storage.init_db()
        super().do_POST()
