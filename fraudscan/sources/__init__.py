"""Registry of available data sources.

Three sources have bespoke field mappings (childcare, contracts, healthcare) and are
classes. Additional categories are config-driven: a source config with
`"kind": "cms_provider"` is served by the generic CmsProviderSource — no new code.
"""
from fraudscan.sources.childcare import ChildCareSource
from fraudscan.sources.contracts import ContractsSource
from fraudscan.sources.healthcare import HealthcareCredentialSource
from fraudscan.sources.cms import CmsProviderSource
from fraudscan.sources.nppes import NppesSource

SOURCE_CLASSES = {
    ChildCareSource.key: ChildCareSource,
    ContractsSource.key: ContractsSource,
    HealthcareCredentialSource.key: HealthcareCredentialSource,
}

# source kinds served by a generic, config-driven class (no per-category code)
CONFIG_KINDS = {"cms_provider", "nppes", "contracts"}


def build_source(key, config):
    scfg = config.get("sources", {}).get(key, {})
    kind = scfg.get("kind")
    if kind == "contracts":
        # another fiscal year of the Agency Contracts series (same schema as pwse-3zea)
        src = ContractsSource(domain=config.get("socrata_domain", "data.wa.gov"),
                              dataset_id=scfg["dataset_id"], where=scfg.get("where"))
        src.key = key                     # instance key so uids don't collide across years
        return src
    if kind == "cms_provider":
        return CmsProviderSource(
            key=key, name=scfg.get("description", key),
            distribution_id=scfg["dataset_id"],
            type_label=scfg.get("type_label", key.replace("_", " ").upper()),
            field_map=scfg["map"], state=scfg.get("state", "WA"))
    if kind == "nppes":
        return NppesSource(
            key=key, name=scfg.get("description", key),
            type_label=scfg.get("type_label", key.replace("_", " ").upper()),
            taxonomies=scfg["taxonomies"],
            entity_kind=scfg.get("entity_kind", "org"),
            state=scfg.get("state", "WA"), cap=scfg.get("cap", 2000))
    cls = SOURCE_CLASSES[key]
    domain = config.get("socrata_domain", "data.wa.gov")
    return cls(domain=domain, dataset_id=scfg.get("dataset_id"),
               where=scfg.get("where"))


def all_source_keys(config):
    """Every source key the CLI knows about (built-in classes + config sources)."""
    return sorted(set(SOURCE_CLASSES) | set(config.get("sources", {})))


def enabled_source_keys(config):
    keys = []
    for k, v in config.get("sources", {}).items():
        if not v.get("enabled", True):
            continue
        if k in SOURCE_CLASSES or v.get("kind") in CONFIG_KINDS:
            keys.append(k)
    return keys
