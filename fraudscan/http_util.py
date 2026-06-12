"""Socrata Open Data API (SODA) client — stdlib only (urllib).

Washington's open data portal (data.wa.gov) runs on Socrata. Datasets are reachable
at https://<domain>/resource/<dataset_id>.json and support paging via $limit/$offset.
An app token (env SOCRATA_APP_TOKEN) is optional but raises throttling limits.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

PAGE_SIZE = 5000
USER_AGENT = "FraudScan/0.1 (public-records research)"


def _request(url, app_token=None, retries=3):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if app_token:
        headers["X-App-Token"] = app_token
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Request failed after {retries} attempts: {url}\n{last_err}")


def soda_fetch(domain, dataset_id, limit=None, app_token=None, where=None,
               progress=None):
    """Return a list of record dicts from a Socrata dataset, paging as needed.

    limit: stop after roughly this many rows (None = all).
    where: optional SoQL $where clause.
    progress: optional callback(count) for status output.
    """
    app_token = app_token or os.environ.get("SOCRATA_APP_TOKEN")
    base = f"https://{domain}/resource/{dataset_id}.json"
    rows = []
    offset = 0
    while True:
        page = min(PAGE_SIZE, limit - len(rows)) if limit else PAGE_SIZE
        params = {"$limit": page, "$offset": offset, "$order": ":id"}
        if where:
            params["$where"] = where
        url = base + "?" + urllib.parse.urlencode(params)
        batch = _request(url, app_token=app_token)
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if progress:
            progress(len(rows))
        if len(batch) < page:
            break
        if limit and len(rows) >= limit:
            break
    return rows


def get_json(url, app_token=None, retries=3):
    """Generic GET → parsed JSON (used for non-Socrata APIs like data.cms.gov)."""
    return _request(url, app_token=app_token, retries=retries)


def post_json(url, body, retries=3, timeout=60):
    """Generic POST of a JSON body → parsed JSON (e.g. USAspending search)."""
    data = json.dumps(body).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
               "Content-Type": "application/json"}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"POST failed: {url}\n{last}")


# ---- CMS Provider Data Catalog (data.cms.gov) ----
CMS_BASE = "https://data.cms.gov/provider-data/api/1/datastore/query"


def _cms_state_params(state):
    return [("conditions[0][property]", "state"),
            ("conditions[0][value]", state),
            ("conditions[0][operator]", "=")]


def cms_provider_fetch(distribution_id, state="WA", limit=None, page=500,
                       progress=None):
    """Page a CMS Provider Data Catalog dataset filtered to one state."""
    base = f"{CMS_BASE}/{distribution_id}"
    rows, offset = [], 0
    while True:
        size = min(page, limit - len(rows)) if limit else page
        params = [("limit", size), ("offset", offset)] + _cms_state_params(state)
        data = get_json(base + "?" + urllib.parse.urlencode(params))
        batch = data.get("results", [])
        rows.extend(batch)
        offset += len(batch)
        if progress:
            progress(len(rows))
        total = data.get("count")
        if len(batch) < size or not batch:
            break
        if limit and len(rows) >= limit:
            break
        if total and offset >= total:
            break
    return rows


def cms_dataapi_fetch(dataset_uuid, filters=None, size=1000, cap=None,
                      progress=None):
    """Page a data.cms.gov *data-api* dataset (e.g. Medicare payment files)."""
    base = f"https://data.cms.gov/data-api/v1/dataset/{dataset_uuid}/data"
    rows, offset = [], 0
    while True:
        params = [("size", size), ("offset", offset)]
        for k, v in (filters or {}).items():
            params.append((f"filter[{k}]", v))
        data = get_json(base + "?" + urllib.parse.urlencode(params))
        if not data:
            break
        rows.extend(data)
        offset += len(data)
        if progress:
            progress(len(rows))
        if len(data) < size or (cap and len(rows) >= cap):
            break
    return rows


def cms_record_url(distribution_id, id_field, id_value):
    """Verifiable per-record link returning that record's JSON."""
    params = [("conditions[0][property]", id_field),
              ("conditions[0][value]", id_value),
              ("conditions[0][operator]", "=")]
    return f"{CMS_BASE}/{distribution_id}?" + urllib.parse.urlencode(params)


# ---- NPPES (NPI registry) via NLM Clinical Table Search Service ----
# npiregistry.cms.gov is not reachable in every environment; the NLM service wraps the
# same NPPES data on a different host and supports exact taxonomy + state filtering.
NLM_BASE = "https://clinicaltables.nlm.nih.gov/api"
_NPPES_DF = ["NPI", "name.full", "provider_type", "addr_practice.line1",
             "addr_practice.city", "addr_practice.state", "addr_practice.zip"]
_NPPES_KEYS = ["npi", "name", "provider_type", "line1", "city", "state", "zip"]


def nppes_fetch(entity_kind, taxonomy, state="WA", cap=2000, page=500, progress=None):
    """Provider records for one NPPES taxonomy in a state (org or individual NPIs)."""
    endpoint = "npi_org" if entity_kind == "org" else "npi_idv"
    url = f"{NLM_BASE}/{endpoint}/v3/search"
    q = f'provider_type:"{taxonomy}" AND addr_practice.state:{state}'
    rows, offset = [], 0
    while True:
        size = min(page, cap - len(rows))
        if size <= 0:
            break
        params = [("terms", ""), ("q", q), ("count", size), ("offset", offset),
                  ("df", ",".join(_NPPES_DF))]
        data = get_json(url + "?" + urllib.parse.urlencode(params))
        total = data[0] if data else 0
        disp = (data[3] if len(data) > 3 else None) or []
        for r in disp:
            rows.append(dict(zip(_NPPES_KEYS, r)))
        offset += len(disp)
        if progress:
            progress(len(rows))
        if not disp or len(disp) < size or offset >= total or len(rows) >= cap:
            break
    return rows


def nppes_provider_url(npi):
    return f"https://npiregistry.cms.gov/provider-view/{npi}"


def dataset_url(domain, dataset_id):
    return f"https://{domain}/d/{dataset_id}"


def record_query_url(domain, dataset_id, field, value):
    """A verifiable deep link returning the raw JSON for a single record."""
    q = urllib.parse.urlencode({field: value})
    return f"https://{domain}/resource/{dataset_id}.json?{q}"
