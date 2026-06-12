"""Vercel Python Function entrypoint."""
import os

os.environ.setdefault("FRAUDSCAN_DATA_DIR", "/tmp/fraudscan-data")

from fraudscan.web.server import Handler  # noqa: E402
from fraudscan import storage  # noqa: E402


class handler(Handler):  # noqa: N801
    def do_GET(self):
        storage.init_db()
        super().do_GET()

    def do_POST(self):
        storage.init_db()
        super().do_POST()
