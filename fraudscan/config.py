"""Config + path helpers. Pure stdlib."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "fraudscan.db")
CONFIG_PATH = os.path.join(ROOT, "config.json")


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR
