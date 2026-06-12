"""Vercel Python Function entrypoint."""
import os

os.environ.setdefault("FRAUDSCAN_DATA_DIR", "/tmp/fraudscan-data")

from fraudscan.web.server import Handler  # noqa: E402


class handler(Handler):  # noqa: N801
    pass
