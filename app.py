"""WSGI entrypoint for Vercel's Python runtime.

The local dashboard uses BaseHTTPRequestHandler so it can stay dependency-free.
Vercel's Python runtime expects a top-level WSGI/ASGI callable named ``app``.
"""
import json
import os
import urllib.parse

os.environ.setdefault("FRAUDSCAN_DATA_DIR", "/tmp/fraudscan-data")

from fraudscan import storage  # noqa: E402
from fraudscan.web import casefile, server  # noqa: E402


def _body(environ):
    try:
        size = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        size = 0
    return environ["wsgi.input"].read(size) if size else b"{}"


def _response(start_response, code, body, ctype="application/json"):
    if isinstance(body, bytes):
        data = body
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = json.dumps(body).encode("utf-8")
    start_response(
        f"{code} {'OK' if code < 400 else 'Error'}",
        [("Content-Type", ctype), ("Content-Length", str(len(data)))],
    )
    return [data]


def _handle_get(path, params):
    if path in ("/", "/index.html"):
        with open(server.INDEX, "rb") as fh:
            return 200, fh.read(), "text/html; charset=utf-8"
    if path == "/api/summary":
        return 200, server._summary(), "application/json"
    if path == "/api/flags":
        return 200, server._flags(params), "application/json"
    if path == "/api/entity":
        ent = server._entity(params.get("uid", [""])[0])
        return (200 if ent else 404), ent or {"error": "not found"}, "application/json"
    if path == "/api/operators":
        return 200, server._operators(params), "application/json"
    if path == "/api/funds_breakdown":
        return 200, server._funds_breakdown(), "application/json"
    if path == "/api/by_county":
        return 200, server._by_county(), "application/json"
    if path == "/api/changes":
        return 200, server._changes(), "application/json"
    if path == "/api/movers":
        return 200, server._movers(), "application/json"
    if path == "/api/search":
        return 200, server._search(params.get("q", [""])[0]), "application/json"
    if path == "/api/random":
        return 200, server._random_lead(), "application/json"
    if path == "/api/operator":
        op = server._operator((params.get("op", params.get("id", [""]))[0]))
        return (200 if op else 404), op or {"error": "not found"}, "application/json"
    if path == "/casefile":
        if params.get("uid", [""])[0]:
            ent = server._entity(params["uid"][0])
            body = casefile.entity_casefile(ent) if ent else "not found"
        else:
            op = server._operator(params.get("op", [""])[0])
            body = casefile.operator_casefile(op) if op else "not found"
        return (200 if body != "not found" else 404), body, "text/html; charset=utf-8"
    return 404, {"error": "not found"}, "application/json"


def _handle_post(path, environ):
    if path != "/api/triage":
        return 404, {"error": "not found"}, "application/json"
    body = json.loads(_body(environ) or b"{}")
    uid = body.get("uid", "")
    if not uid:
        return 400, {"error": "uid required"}, "application/json"
    state = body.get("state")
    if state not in (None, "", "reviewed", "dismissed", "escalated"):
        return 400, {"error": "bad state"}, "application/json"
    conn = storage.connect()
    try:
        storage.set_triage(
            conn,
            uid,
            state=state,
            note=body.get("note"),
            watched=body.get("watched"),
        )
        row = conn.execute(
            "SELECT state, note, watched FROM triage WHERE entity_uid=?", (uid,)
        ).fetchone()
        return 200, dict(row) if row else {}, "application/json"
    finally:
        conn.close()


def app(environ, start_response):
    storage.init_db()
    path = environ.get("PATH_INFO") or "/"
    params = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    try:
        if environ.get("REQUEST_METHOD", "GET").upper() == "POST":
            code, body, ctype = _handle_post(path, environ)
        else:
            code, body, ctype = _handle_get(path, params)
    except Exception as exc:
        code, body, ctype = 500, {"error": str(exc)}, "application/json"
    return _response(start_response, code, body, ctype)
