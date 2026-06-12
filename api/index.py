"""Vercel Python Function entrypoint."""
import os

os.environ.setdefault("FRAUDSCAN_DATA_DIR", "/tmp/fraudscan-data")

from fraudscan.web.server import Handler as handler  # noqa: E402,N812
