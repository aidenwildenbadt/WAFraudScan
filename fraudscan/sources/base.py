"""Common entity model + base class for Socrata-backed data sources."""
from dataclasses import dataclass, field, asdict
from typing import Optional

from fraudscan import http_util


@dataclass
class Entity:
    """A normalized record from any source (a provider, contractor, etc.)."""
    source: str
    source_id: str
    name: str = ""
    dba: str = ""
    entity_type: str = ""
    status: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    county: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    amount: Optional[float] = None      # dollar magnitude where applicable
    source_url: str = ""                # verifiable link to the public record
    raw: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.source_id}"

    def as_row(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        d["uid"] = self.uid
        return d


class SocrataSource:
    """Subclass per dataset: set key/name/dataset_id and implement to_entity()."""
    key: str = ""
    name: str = ""
    dataset_id: str = ""

    def __init__(self, domain, dataset_id=None, where=None):
        self.domain = domain
        if dataset_id:
            self.dataset_id = dataset_id
        # Optional SoQL $where to bound a large dataset to the relevant slice.
        self.where = where

    def fetch(self, limit=None, progress=None):
        return http_util.soda_fetch(
            self.domain, self.dataset_id, limit=limit, where=self.where,
            progress=progress,
        )

    def to_entity(self, rec: dict) -> Optional[Entity]:
        raise NotImplementedError

    def entities(self, limit=None, progress=None):
        out = []
        for rec in self.fetch(limit=limit, progress=progress):
            ent = self.to_entity(rec)
            if ent is not None:
                out.append(ent)
        return out

    # ---- small parsing helpers shared by subclasses ----
    @staticmethod
    def _f(rec, key):
        v = rec.get(key)
        if v in (None, "", "NULL"):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _s(rec, key):
        v = rec.get(key)
        return str(v).strip() if v not in (None, "") else ""

    @staticmethod
    def _money(rec, key):
        """Parse currency strings like '$   100,000.00' into a float."""
        v = rec.get(key)
        if v in (None, "", "NULL"):
            return None
        cleaned = str(v).replace("$", "").replace(",", "").strip()
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            return None
