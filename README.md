# FraudScan

**Surface anomaly / fraud-risk _leads_ in Washington State public spending — ranked for human review.**

FraudScan pulls publicly available Washington data (the open-data portal at
[data.wa.gov](https://data.wa.gov), which runs on Socrata), applies a set of
transparent, explainable rules, and produces a ranked "review queue" of records that
are statistically unusual in ways associated with misuse of public money — for
example, licensed child-care providers receiving subsidy payments.

> [!IMPORTANT]
> **These are investigative leads, not findings of fraud.** A high score means a
> record is unusual and worth a closer look. It is **not** evidence of wrongdoing.
> Statistical outliers have innocent explanations (large legitimate operators, data-
> entry quirks, etc.). Every flag links back to the underlying **public record** so a
> human can verify it. Use this to *prioritize where to look*, never to accuse.

It is the kind of analysis auditors, investigative journalists, and watchdog groups
already do — made repeatable and transparent.

---

## Why it can do this

Washington publishes the raw material:

| Source | Dataset | What it gives us |
|---|---|---|
| **DCYF Licensed Child Care Providers** | [`was8-3ni8`](https://data.wa.gov/d/was8-3ni8) | ~2,500 active providers with license status/dates, capacity, geocoded address, contact info, and the **SSPS provider number** (the Social Service Payment System id used to pay subsidies). |
| **WA Agency Contracts** | [`pwse-3zea`](https://data.wa.gov/d/pwse-3zea) | State agency contracts with vendor, purpose, and dollar amounts. |
| **DOH Health Care Provider Credentials** | [`qxh8-f4bd`](https://data.wa.gov/d/qxh8-f4bd) | Every WA health-care credential with status, expiration, and whether a disciplinary action was taken. Used for **provider sanction screening**. Ingest is bounded by a SoQL filter to the integrity-relevant slice (~36k of 2.4M rows). |
| **CMS facility categories** (federal) | [data.cms.gov](https://data.cms.gov/provider-data/) Provider Data Catalog | Medicare-certified facilities filtered to WA, with address, ownership, and certification date: **Hospice** (50), **Home Health agencies** (71), **Dialysis facilities** (114), **Skilled Nursing Facilities** (194). Config-driven — adding a category is a config block, not code. |
| **Exclusion / sanction lists** | [OIG LEIE](https://oig.hhs.gov/exclusions/) (83k) + [CMS Revoked](https://data.cms.gov) + [SAM.gov](https://sam.gov) debarment (9k) | Parties barred from federal programs. Matched by NPI and name+state. SAM is the downloadable Public V2 extract dropped in `data/screening/`. |
| **CMS ownership** (nursing) | [data.cms.gov](https://data.cms.gov) SNF change-of-ownership + owner disclosure | Ownership churn + owner→facility networks for nursing homes (WA-bounded). |
| **NPPES categories** (federal, Medicaid-relevant) | NPI registry via [NLM Clinical Tables](https://clinicaltables.nlm.nih.gov/) | Provider types billed to Medicaid but absent from Medicare data, pulled by NPI taxonomy + state: **ABA/autism — Behavior Analyst** orgs (232), **Non-emergency Medical Transport** (117), **Durable Medical Equipment** (112). Config-driven by taxonomy code. |
| **Business registry** (cross-check) | DOR Business Lookup / SoS Corporations | Loaded from a CSV you export (see below) — used to flag providers/vendors with no *active* registration. |

The child-care dataset is the anchor because it ties licensing to the subsidy payment
system — the exact place "illegitimate daycare" schemes against public money live.

### A note on data access (important, honest)

A few things WA does **not** publish as bulk/API data, which shaped the design:

- **Business registrations.** Both the DOR Business Lookup (`4wur-kfnr`) and SoS
  Corporations Search (`f9jk-mm39`) on data.wa.gov are just `href` links to web apps;
  the CCFS API is bot-protected. We deliberately **do not circumvent** that. Instead
  the registry cross-check loads a CSV you export from either portal (both support
  export). See [Business-registration cross-check](#business-registration-cross-check).
- **Medicaid/Apple Health enrolled providers.** There is no open dataset of Apple
  Health *enrolled* providers or payments — that lives in HCA's ProviderOne/dashboards.
  The closest public analog is the DOH **credential** data, which we use for sanction
  screening (revoked/suspended/disciplined providers who may still be billing). True
  payment-level Medicaid analysis needs HCA data via a public-records request (roadmap).
- **Facility categories** (hospice, home health, dialysis, nursing, …) are **not** on
  data.wa.gov. They come from the **federal CMS Provider Data Catalog** (data.cms.gov),
  filtered to WA — same facilities, federal provenance (shown in the UI).
- **ABA/autism and non-emergency medical transport (NEMT)** are Medicaid-only provider
  types, absent from Medicare/CMS facility data. They live in the **NPPES NPI registry**
  by taxonomy code. The usual host (`npiregistry.cms.gov`) is DNS-blocked in some
  environments, so we read the same NPPES data through the **NLM Clinical Table Search
  Service** (`clinicaltables.nlm.nih.gov`), filtered by exact taxonomy + state. Shipped.

## Zero dependencies

Pure Python standard library — `urllib` (ingest), `sqlite3` (storage), `http.server`
(dashboard). **No `pip install` required.** Tested on Python 3.11+.

## Quick start

```bash
cd FraudScan

# 1. Pull public data + run the rules (writes data/fraudscan.db)
python3 -m fraudscan run            # ingest all + score + resolve + payments
#   ...or step by step:
python3 -m fraudscan ingest         # pull data into the local DB
python3 -m fraudscan score          # run rules + scoring
python3 -m fraudscan resolve        # cluster entities into cross-source operators
python3 -m fraudscan payments       # attach public $ amounts + time periods
python3 -m fraudscan discipline     # ingest DOH disciplinary narratives → data/context/
python3 -m fraudscan childcare-enforcement  # scrape findchildcarewa complaints/inspections

# 2. Explore the ranked review queue + operators view
python3 -m fraudscan serve          # http://127.0.0.1:8000
```

Other commands:

```bash
python3 -m fraudscan sources                 # list data sources
python3 -m fraudscan ingest --source childcare --limit 500
python3 -m fraudscan score  --source childcare
python3 -m unittest discover -s tests        # run the test suite
```

An optional `SOCRATA_APP_TOKEN` env var raises Socrata's rate limits (not required for
these dataset sizes).

## How it works

```
data.wa.gov (Socrata)
      │  fraudscan/sources/*   normalize each dataset → Entity
      ▼
  SQLite (data/fraudscan.db)   entities · flags · scores · operators
      │  fraudscan/rules/*     explainable rules → Flag (with evidence)
      │  fraudscan/scoring.py  sum severities → risk_score (capped at 100)
      │  fraudscan/resolve.py  cluster entities → cross-source operators
      ▼
  fraudscan/web/               http.server + dashboard (review queue + operators)
```

- **Sources** (`fraudscan/sources/`) map a raw Socrata record to a normalized
  `Entity` and provide a verifiable link back to the source record.
- **Rules** (`fraudscan/rules/`) operate over the *whole* set for a source so they can
  catch cross-record patterns, not just per-row checks. Each `Flag` carries the
  specific evidence that triggered it.
- **Scoring** ([scoring.py](fraudscan/scoring.py) + [taxonomy.py](fraudscan/taxonomy.py))
  is transparent and *de-correlated*: `risk = min(100, Σ over correlation-groups of the
  MAX severity in that group)`, so the same underlying fact isn't double-counted (a
  suspended credential and its disciplinary action are one event). It tracks how many
  independent signal *families* (integrity / network / billing / quality) an entity
  spans and uses that as the sort tie-breaker — a multi-family lead outranks a one-family
  pile-up. **Legitimacy suppression** ([legitimacy.py](fraudscan/legitimacy.py))
  halves the score of obvious institutions (school districts, Head Start, YMCA, tribes)
  so they don't saturate the top. It orders the queue; it is not a probability.
- **Resolution** (`fraudscan/resolve.py`) clusters entities across sources that share a
  normalized business name, contact person, contact info, address, or fuzzy name into a
  single *operator* (see below). It writes each member's `operator_id` back onto the
  `entities` table, so the entity-centric review queue can flag rows that belong to an
  operator inline.

All thresholds and severities live in [`config.json`](config.json) — tune them without
touching code.

## Current rules

**Child care** (`fraudscan/rules/childcare.py`)

| Rule | Lead it surfaces |
|---|---|
| `license_expired_active` | License expired while operating status is still "Active". |
| `shared_contact_multiple_providers` | A phone/email shared by a *small* cluster of differently-named providers (possible common operator behind separate registrations). Large clusters = known chains, excluded. |
| `address_shared_multiple_providers` | Multiple distinct providers (different names) at one physical address. |
| `concentration_same_contact_person` | One contact person listed across many (but not chain-scale) facilities. |
| `capacity_outlier_high` | Licensed capacity far above the norm for its facility type (>3σ). |
| `recent_license_high_capacity` | Newly licensed with top-decile capacity. |
| `capacity_missing_or_zero` | Active provider with no licensed capacity on file. |
| `missing_contact_info` | Active provider with neither email nor phone. |
| `ungeocoded_address` | Active provider whose address the state couldn't geocode (low-weight context). |
| `payment_only_certificate` | "Payment only" certificate — paid via subsidy without a standard license (low-weight context). |
| `misspelled_word_in_name` | A name contains a likely misspelling (e.g. `Transpot`→`Transport`, `Behaviorial`→`Behavioral`) — hasty/shell registration or a near-copy of a legitimate name. See below. |
| `childcare_valid_complaint` | One or more **valid/investigated complaints** on DCYF "Child Care Check" (scraped per-provider from findchildcarewa by WACOMPASS id). Substantiated complaints are uncommon → high-signal. Childcare's analog to nursing's CMS enforcement, which WA doesn't publish as open data. `childcare_many_inspections` is the routine-inspection count (context). Run `python3 -m fraudscan childcare-enforcement` to populate (cached/resumable). |

**Misspelled-word detection** (`fraudscan/rules/naming.py`, applied to child care and
all facility categories): data-driven Norvig-style spell-check, no third-party
dependency. The "correct" vocabulary is learned from the frequency of words across all
names, **seeded** with ~100 domain, quality, and commonly-misspelled words (so
`Profesional→Professional`, `Comunity→Community`, `Independant→Independent`,
`Acheivement→Achievement`, `Progam→Program` are caught). A *rare* token that is **one
edit** (insert/delete/replace/transpose — these are 80–95% of real typos) from a known
word is flagged; for long tokens (≥8) a **2-edit fallback** runs against a tight curated
anchor list of long, frequently-misspelled words (catches `Acomodation→Accommodation`).

Precision is the hard part, and three guards keep false positives near zero:
- **Real English words are skipped** (system word list `/usr/share/dict/words` if
  present) — so `Peace`, `Palace`, `iCare` aren't flagged.
- **Plural/singular pairs and corrections under 5 letters are skipped** (no `Lakes→Lake`,
  no `iCare→Care`).
- **2-edit only against the curated anchors** — generic 2-edit matching wrongly mapped
  `Medicare→Medical`, `Brighton→Bright`, `Tendercare→Kindercare`, `Pursuing→Nursing`
  (the word list lacks inflected forms and proper nouns), so it's deliberately narrow.

Tunable in `config.json` (`severity`, `common_min`, `rare_max`, `min_len`,
`min_target_len`, `max_edits`, `edit2_min_len`).

**Contracts** (`fraudscan/rules/contracts.py`) — classic procurement-audit heuristics:
`contract_duplicate_amount_vendor` (possible split/double billing),
`contract_round_large_amount` (estimate-based rather than itemized).

**Health care — provider sanction screening** (`fraudscan/rules/healthcare.py`)

| Rule | Lead it surfaces |
|---|---|
| `credential_revoked_or_suspended` | Credential is Revoked/Suspended — provider isn't authorized to practice; verify they aren't still billing. |
| `disciplinary_action_taken` | A disciplinary action is on record. |
| `credential_active_with_conditions` | Practicing under conditions/restrictions. |
| `credential_surrendered` | Credential surrendered (often resolves a complaint/investigation). |
| `disciplinary_action_pending` | A disciplinary action is pending. |

**Cross-source & global** (`fraudscan/rules/cross.py`) — these use shared reference data
loaded into the scoring context (registry, exclusion lists, payments, ownership):

| Rule | Family | Lead it surfaces |
|---|---|---|
| `no_active_business_registration` | integrity | Provider/vendor with no *active* match in the loaded business registry. Child care + contracts. |
| `excluded_or_sanctioned` | integrity | Matches a **federal exclusion/sanction list** (OIG LEIE, CMS Revoked, SAM). Tiered by identity confidence — **definitive** (NPI, 40; +mandatory exclusion → 48), **corroborated** (name+state + city/type aligns, 30), **name-only** (20, verify; city mismatch flagged as possible namesake) — and by exclusion authority (mandatory `1128(a)` > permissive `1128(b)`). Runs on every source. |
| `payment_anomaly` → `billing_spike` / `payment_outlier` | billing | Year-over-year payment jump (≥2×) or total payments in the top 5% of a program's peers, from the public payment data. |
| `paid_while_sanctioned` | billing | **The strongest lead:** a provider whose credential is revoked/suspended/expired but who is still drawing public money (Medicare, matched via the NPI crosswalk). E.g. *John Morrison — Revoked, $362K Part D → score 97.* |
| `low_quality_rating` | corroboration | CMS Care Compare quality ≤2 of 5 stars (home health, dialysis, nursing). |
| `ownership_churn` | network | Nursing home that underwent a Medicare change-of-ownership (CHOW) — churn is an asset-stripping / license-laundering signal. |
| `shared_owner` | network | One owner linked to multiple nursing facilities (ownership-network concentration). |

> **Reliability-weighted on purpose.** An NPI match to an exclusion list (40) outweighs
> a soft anomaly like for-profit ownership (4–5); name-based matches are explicitly
> lower-confidence "verify identity" leads. This mirrors how program-integrity teams
> stack independent signals — the operator score is where they combine.

> Calibration note: rules that detect "shared identity" are deliberately capped to
> exclude large legitimate operators (e.g. a national chain showed up at 214 sites,
> a YMCA at 29). Without the cap, those would bury the genuinely small, unusual
> clusters the tool is meant to find.

### Exclusion / sanction screening + ownership (this build)

The #1 program-integrity check ([OIG](https://oig.hhs.gov/exclusions/)) is now built in
([`screening.py`](fraudscan/screening.py)): on each `score`, FraudScan loads the **OIG
LEIE** (~83k excluded parties, cached locally) and **CMS Revoked Medicare** providers,
indexes them by NPI and name+state, and flags any matching entity/operator. Live, this
surfaced **1,167** WA providers whose names match a federal exclusion — overwhelmingly
*already* in our DB for a state-sanctioned credential, i.e. corroborated.
**NPI crosswalk → "still being paid?"** DOH credential data has no NPI, so sanctioned
*physicians/prescribers* (physicians, dentists, pharmacists, ARNPs, …) are resolved to
their NPI via the NLM/NPPES service ([crosswalk.py](fraudscan/crosswalk.py), cached &
resumable). The NPI then (a) **confirms exclusions by NPI** (definitive identity, not
just name+state — 6 confirmed so far, filtering namesakes) and (b) joins **Medicare Part
B + Part D**, producing the `paid_while_sanctioned` lead. Built for the high-yield
population: it is deliberately *not* run on ABA/NEMT/DME (those are Medicaid categories
that don't bill Medicare — 0/461 of their NPIs appear in Part B — and already match
exclusions by NPI directly).

*SAM.gov debarment* is wired in too: download the **Exclusions Public Extract V2** from
SAM.gov (Data Services → File Extracts → Exclusions → Public V2 — no API key needed for
the file), drop the `.CSV` in `data/screening/`, and the loader auto-detects the SAM V2
schema (firm vs. individual names, active-only via `Record Status`, NPI + name/state).
Live it added **26** federal-debarment hits beyond LEIE/CMS. Refresh by replacing the file
with a newer dated extract.

Ownership ([`ownership.py`](fraudscan/ownership.py)) uses CMS's only open ownership data
— SNF change-of-ownership + owner disclosure — so the **nursing** category was added to
have something to join to (**82** CHOW facilities, **5** multi-facility owners found,
WA-bounded). Out-of-state owners of WA homes aren't captured (logged, not hidden).

> Calibration note: rules that detect "shared identity" are deliberately capped to
> exclude large legitimate operators (e.g. a national chain showed up at 214 sites,
> a YMCA at 29). Without the cap, those would bury the genuinely small, unusual
> clusters the tool is meant to find.

## Sharper, more decisive leads (latest build)

Six upgrades aimed at the same goal: move a lead from "looks risky" to "same person, and
money actually moved after a real bar" — with an evidence chain a skeptic can't wave away.

1. **Identity-confirmed exclusion matches + exclusion-type tiering**
   ([screening.py](fraudscan/screening.py)). The OIG LEIE download carries far more than a
   name: **NPI, DOB, full address, and an exclusion-authority code** — and the DOH
   credential file carries **birth year**. Matches are graded — **definitive** (NPI on the
   exclusion record), **corroborated** (name+state *and* the **birth year**, practice city,
   or provider type aligns), or **name-only** (verify). A confident **birth-year/DOB
   mismatch drops the match entirely** as a namesake. Severity is also tiered by authority:
   a **mandatory** `1128(a)` conviction-based exclusion outranks a permissive `1128(b)`
   license action. Live effect on the WA exclusion matches: **1,013 corroborated / 72
   NPI-definitive / only 46 name-only**, with **76 namesake matches dropped** by birth-year
   mismatch — a direct hit on the false-positive problem.

2. **Auto-assembled evidence dossier** ([server.py](fraudscan/web/server.py) `_build_dossier`).
   Each lead's detail view opens with a four-step chain — **Identity → The bar → The
   money → The contradiction** — each step strength-coded, plus a **"confirms if / refutes
   if"** checklist (e.g. *"match the LEIE DOB to this provider; if it differs, it's a
   namesake — dismiss"*). Turns a risk score into something an investigator can act on.

3. **Multi-year Medicare (2019–2023)** ([config.json](config.json) `payments`). Part B and
   Part D are pulled for five years (stable per-year CMS dataset UUIDs, with tolerant
   field-name casing across years). This **widened the `paid_after_barred` window from 4 to
   22 leads** ($62K → **$5.6M** verifiable-contradiction slice) and powers a **funding time
   series** on the Funding tab. Visible Medicare money grew accordingly — Part D
   $113M→**$562M**, Part B $35M→**$243M** (five years vs one).

4. **FAC single-audit findings — an *independent* signal** ([fac.py](fraudscan/fac.py)).
   For operators with an EIN (from their IRS-990 match), FraudScan queries the **Federal
   Audit Clearinghouse** for the most recent Single Audit and surfaces its **findings**
   (questioned costs, material weakness, modified opinion, repeats) and the **federal $ on
   programs that drew findings**. Unlike our heuristics, these are an *independent
   auditor's* determinations — the closest public data gets to verified misuse. Shown as a
   ⚖ badge + audit panel on operators, and a slice on the Funding tab. *Needs a free
   [api.data.gov](https://api.data.gov/signup) key in `FAC_API_KEY` for full coverage; the
   built-in DEMO_KEY is ~30 req/hr and caches by EIN as quota allows.*

5. **WA SOS business-identity linking** ([sos.py](fraudscan/sos.py)). Links entities that
   share a **UBI**, **registered agent**, or **governing officer** — far stronger than
   fuzzy name + geo, and the cleanest shell-network signal. WA SOS retired its free bulk
   extract and the CCFS API is bot-gated (we don't circumvent it), so this loads a CSV you
   export from the **CCFS Advanced Business Search** (the green CSV icon) into `data/sos/`;
   columns are auto-detected. Off until a file is present.

6. **CMS ownership + billing-group operator edges** ([ownership.py](fraudscan/ownership.py),
   [reassignment.py](fraudscan/reassignment.py)). Ownership linking now spans **SNF +
   hospice + HHA** "All Owners" files (keyed by the owner's PECOS associate id), so
   facilities under a **shared beneficial owner** merge into one operator and `shared_owner`
   fires for hospice/home-health too. The **Revalidation Reassignment** file links
   providers who **reassign Medicare billing to the same group** — a documented financial
   relationship, not a name guess.

**Crosswalk identity confirmation (closing the last gap).** The high-dollar
`paid_while_sanctioned` leads hinge on a second identity hop — *is the NPI that received
the Medicare money really the sanctioned credential-holder?* (DOH→NPI name match). We now
grade that hop with hard identifiers, strongest first
([crosswalk.py](fraudscan/crosswalk.py) + [rules/cross.py](fraudscan/rules/cross.py)):

- **license-confirmed** — the DOH **credential number** equals the **NPPES license
  number** for the resolved NPI (both carried in the data; matched on the numeric core).
  A near-decisive identity link.
- **dob-confirmed** — the resolved NPI is on an exclusion list *by NPI* and its **DOB
  matches the DOH birth year**.
- **city-corroborated / exclusion-npi** — NPPES and the exclusion record agree on city, or
  the NPI is federally listed under the same name.
- **unique-name** — only the unique name+state crosswalk (verify), and **license-/dob-
  mismatch** is surfaced as a likely namesake.

The dossier's Identity step shows this grade, and "confirms/refutes if" adapts to it (a
license-confirmed lead no longer says "verify identity").

**Down-weighting namesakes + de-saturating the rankings.** A `license-mismatch` /
`dob-mismatch` means the resolved NPI is a *different* person, so every NPI-derived flag
is mis-attributed: `paid_while/after_sanctioned` collapse to a low-severity
`paid_attribution_unconfirmed` (8), the NPI-based exclusion drops to name-only weight, and
billing forensics is skipped. Live, this removed **9 false "paid-after-barred"** leads and
reclassified **75 namesake** leads ($33M) off the top. Because the record score is a
severity sum that pins many sanctioned providers at 100, the **review queue** then
tie-breaks the 100s by *dated contradiction → dollars at stake → signal-family count*, and
**operators** score by worst-member-plus-bonuses (not a sum) — so confirmed, high-dollar,
dated contradictions surface first instead of a flat wall of 100s. To light these up on an existing
DB: `python3 -m fraudscan crosswalk --refresh-detail` (backfills NPPES practice city **and
license number**), then re-run `payments` → `score` → `resolve`.

## Context & coverage (the story behind a flag)

A flag says *what's unusual*; an investigator needs *what actually happened*. Each
provider's dossier now carries a **Context & coverage** section drawn from three layers
([context_sources.py](fraudscan/context_sources.py), [doh_discipline.py](fraudscan/doh_discipline.py)):

1. **Auto-ingested DOH disciplinary narratives** (`python3 -m fraudscan discipline`). Crawls
   the DOH Newsroom *"State disciplines health care providers"* archive, parses each
   `Name (CREDENTIAL#)` entry plus its plain-English reason ("agreed order requiring a
   $5,000 fine and an ethics assessment"), and matches it to our flagged providers by the
   credential **digit-core**. Live: **540** flagged providers matched. Writes
   `data/context/doh_discipline.csv` (re-run to refresh; raw posts cached).
2. **Curated context you drop in** — a CSV in `data/context/` (`credential` / `npi` /
   `name` → `url`, `title`, `date`, `kind`). This is where ad-hoc finds live: a news
   investigation, an agency update, a court order. Recorded once, it surfaces on that
   provider forever. See [`examples/context_sample.csv`](examples/context_sample.csv).
   (Matched by credential digit-core, NPI, or name — add a credential/NPI to pin a
   common name.)
3. **On-demand source pointers** — every dossier links to the **DOH Provider Credential
   Search** (the authoritative record + legal-order PDFs), the **NPPES** record (when an
   NPI is known), and a prebuilt **news/web search** for that provider. For **child care**,
   it deep-links the provider's **[findchildcarewa](https://www.findchildcarewa.org/)** page
   — Early Achievers rating, **complaints, inspections, and license history** (the
   enforcement context not published as open data) — keyed by our `source_id`, which is the
   portal's own WACOMPASS provider id. We link to the source; we never auto-assert a match.

> Honest limits: the DOH credential portal is session-based, so we link to it rather than
> deep-link the exact PDF; news is curated/on-demand, never auto-matched.

## Business-registration cross-check

WA's business registries are export-gated (not bulk APIs), so the cross-check loads a
CSV you export, then turns on automatically.

1. Export registered businesses to CSV from either portal:
   - **DOR Business Lookup** → <https://secure.dor.wa.gov/gteunauth/?Link=Lookup>
     (search broadly, then export results), or
   - **SoS Corporations Search** → <https://ccfs.sos.wa.gov/#/AdvancedSearch> (Advanced
     Search → export to Excel, save as CSV).
2. Drop the file(s) in `data/registry/` (any filename ending in `.csv`).
3. Re-run `python3 -m fraudscan score`. You'll see
   `Business registry loaded: N rows … cross-check ON`.

The loader auto-detects name/trade-name and status columns (override in
`config.json → registry`). See [`examples/business_registry_sample.csv`](examples/business_registry_sample.csv)
for the expected shape.

> **Completeness matters.** "No registration found" is only meaningful with a *complete*
> registry — a partial export makes everything look unregistered. The
> "found-but-inactive" signal (a closed/revoked match) is robust even with partial data,
> which is why it carries the higher severity.

## Cross-source operators

The **Operators** tab answers a different question than the review queue: not "which
*record* is unusual?" but "which *operator* is unusual?" `resolve` clusters entities
that resolve to the same real-world operator (union-find over namespaced match keys):

| Link | Connects |
|---|---|
| `org:<name>` | business name / DBA across any org-named source (child care, contracts, hospice, home health, dialysis) |
| `person:<name>` | child-care primary contact ↔ health-care credential holder |
| `addr:<address>` | providers at the same physical street address — any address-bearing source (child care + CMS facility categories), with USPS-style abbreviation standardization so `123 Main Street` == `123 MAIN ST` |
| *geo-proximity* | child-care providers whose **geocodes are within ~50m** but with different names *and* street addresses — catches one site/complex registered under different addresses (e.g. `Bldg A` / `Bldg B`, `321` / `345` of the same road). Spatial grid-bucketed + haversine, so it isn't O(n²). |
| `email:` / `phone:` | shared contact within child care |
| *fuzzy* | near-duplicate **business names** (typos, `LLC` suffix, dropped words, numbered branches) via `difflib`, found by blocking on shared substantial words to stay tractable |

An operator is surfaced when it is **cross-program** (2+ sources) **or** a
single-program **consolidation** — multiple differently-named registrations at one
address, or name variants linked by fuzzy match.

> **Ranking & actionability (latest build).** The operator score is the **worst single
> member + structured bonuses** (verifiable contradiction, barred members who drew public
> $, hard-identity link), **not the sum of members** — summing pinned every large cluster
> at 100. Each operator carries a **risk rollup**: `barred_members`, `sanctioned_members`,
> `contradiction_amount` ($ paid after a bar), `dollars_at_stake`, and `strongest_link`
> (hard owner/UBI > shell agent/officer > address > geo > fuzzy). The list shows these as
> badges; the detail view is an **operator dossier** — risk summary, "why linked," a
> **what-to-verify** checklist, and members sorted barred-first with each member's top flag
> + identity confidence. **Filter** by *barred member / verifiable contradiction /
> cross-program / hard link / soft-only* and **sort** by $ at stake or barred count. Large
> clean networks (no barred member) are suppressed as likely chains. **Reassignment**
> ("reassigns Medicare billing to a common group") was **removed as a merge edge** — it
> chained unrelated providers through hospital billing systems (Swedish, MultiCare, Kaiser)
> into false 100+-member blobs; shared employer ≠ shared operator.

Each is scored and shown with every member so a person can confirm. Live finds:

- Two daycare registrations under slightly different names → **one contact person with a
  *suspended* medical credential**; daycare contacts matching **revoked** credentials.
- Organizations running **both licensed daycares and state contracts** (e.g. Head Start).
- Three differently-named daycares at **one street address**; chains and renames caught
  by fuzzy match (`Little Scholars Development Center` vs `… Center LLC`,
  `Childs Time II … IX`).
- **Geocode-proximate** clusters at different street addresses: `Unity Childrens
  University / Ministries / Children` at 6202 / 6001 / 6017 E McKinley Ave; `Hugs Tugs &
  Luv` across `Bldg A / B / C` of one complex.
- Co-located **hospice + home-health** operators (Assured, Encompass, Elite, Harbors) —
  the classic combined-agency pattern — resolved across the two CMS categories by shared
  address and name.
- **Four differently-named Medicaid billers at one Spokane suite** — two NEMT companies
  (DK Reliable, ProMed) *and* two ABA providers (HelpGrow, Poppy Consulting) at 522 W
  Riverside Ave Ste N — an `aba + nemt` operator no single-category view would surface.

Membership is also surfaced **inline in the review queue**: any flagged record that
belongs to an operator shows a `⛓ operator · N` badge (click it to open the operator),
and the entity detail shows a "Part of operator …" banner. A **"in an operator"** filter
narrows the queue to those records. This is powered by the `entities.operator_id` column
written during `resolve` (added automatically to existing databases via migration).

> Guardrails: a key shared by more than `resolve.max_key_members` entities (default 8)
> is treated as generic/chain and skipped (logged); fuzzy matching is limited to
> business names, blocked on shared words, with a `resolve.fuzzy_threshold` (default
> 0.9) similarity floor. **Names and addresses still collide** — every link is a lead to
> verify, not proof. The operator view shows the full member list precisely so a human
> can make that call. Tunable in [`config.json → resolve`](config.json).

## Dollar amounts & time periods

The Operators tab, review queue, and entity detail surface **how much public money a
provider/operator received and over what period** — but only where that data is
genuinely public ([`payments.py`](fraudscan/payments.py)):

| Money source | Coverage | Period |
|---|---|---|
| **WA agency contracts** | full (amount + effective dates, already ingested) | contract start → end |
| **Medicare DME** (data.cms.gov, by NPI) | partial — only suppliers that bill Medicare above CMS's publication threshold (7 of our 112 DME providers matched, but with real $: one shows **$4.1M across 2022–2023**) | per calendar year |
| **Medicare Part B / Part D** (data.cms.gov, by NPI via crosswalk) | sanctioned physicians/prescribers resolved to NPIs ([crosswalk.py](fraudscan/crosswalk.py)) then joined: **$2.0M Part B + $4.9M Part D** across sanctioned providers | per calendar year |
| **Federal awards** ([USAspending.gov](https://api.usaspending.gov)) | operator-level grants + contracts by recipient name (e.g. Rural Resources Community Action: **$36M**) | award periods |
| **IRS Form 990** ([ProPublica](https://projects.propublica.org/nonprofits/)) | operator-level nonprofit total revenue + EIN (e.g. **$10.2M** revenue) — also an "established nonprofit" legitimacy signal | latest filing |

The dashboard shows a **"Public $ surfaced"** card, a green `$` pill on rows/operators,
and a per-period breakdown in each entity's detail. Operators roll up their members' $.

> **The big honest gap:** the money that matters most for *these* categories is
> **Medicaid/state subsidy** — WA child-care subsidy (WCCC) and Apple Health payments for
> ABA, NEMT, hospice, home health. **None of it is public per-provider** (it lives in
> DCYF/HCA's ProviderOne). Surfacing it requires a public-records request. Tellingly, the
> highest-*risk* flagged providers (child care, sanctioned credentials) are exactly the
> ones with no public payment data — so a high score + "no public $" is itself a reason to
> file a records request for that provider's payment history.
>
> Adding a payment source is config (`config.json → payments`): a CMS data-api dataset by
> NPI, or a CSV of payment data obtained via records request.

## Roadmap / extension path

The architecture is built to add sources and cross-source rules.

**Done:** child care, contracts, health-care sanction screening, CMS facility
categories (hospice, home health, dialysis, **nursing**), NPPES categories (ABA/autism,
NEMT, DME), the business-registration cross-check, cross-source operator resolution,
public dollar-amount surfacing (contracts + Medicare + **USAspending federal awards** +
**IRS 990** nonprofit revenue), **federal exclusion/sanction screening** (OIG LEIE + CMS
Revoked + SAM.gov), **payment-anomaly** flags, **CMS quality** flags, **nursing-home
ownership** signals, **de-correlated, diversity-weighted scoring with known-institution
suppression**, an **NPI crosswalk** (sanctioned physicians → NPI → Medicare Part B/D +
NPI-confirmed exclusions), and the **`paid_while_sanctioned`** composite lead.

**High-value next steps:**

0. **More categories — config only.** CMS: nursing homes, inpatient rehab, FQHCs (a
   `cms_provider` block each). NPPES by taxonomy: Behavior Technician (`106S00000X`),
   Ambulance (`341600000X`), Home Infusion, Adult Day Care, etc. (an `nppes` block each,
   `entity_kind` org or individual). No code needed.
1. **Working Connections Child Care (WCCC) subsidy payments** — join licensing to
   actual dollars paid via the SSPS provider number to flag billing > capacity, or
   payments to lapsed/closed providers. (Payment-level data is held by DCYF/SSPS;
   obtainable via public-records request.)
2. **Apple Health (Medicaid) payments/enrollment** — the credential data gives sanction
   screening today; joining to ProviderOne payment data (HCA, via records request)
   would surface revoked/suspended providers who are *still billing* — the real fraud.
3. **Automated registry refresh** — if WA later exposes a bulk business-registry feed,
   add a `SocrataSource`-style loader so the cross-check needs no manual export.
4. **Open Checkbook vendor payments** ([fiscal.wa.gov](https://fiscal.wa.gov/Spending/Checkbook))
   — payment-level expenditure anomalies.
5. **Sharper entity resolution** — the operator view matches on name/contact today;
   add address-based matching and fuzzy name matching, and fold the registry into the
   operator graph as a node rather than just an annotation.

**Adding a category is usually just config.** A new CMS Provider Data Catalog category
needs only a `config.json → sources` block with `"kind": "cms_provider"`, the dataset
distribution id, a column `map`, and `"rules_profile": "facility"` — no code. (For a
genuinely new shape — a different API or field semantics — add a source class in
`fraudscan/sources/` and, if needed, a rule profile in `fraudscan/rules/`.) Storage,
scoring, resolution, and the dashboard pick it up automatically; address-bearing
sources auto-join address/geo matching.

## Responsible use

- Treat output as **leads to verify**, never conclusions. Always open the linked
  source record.
- Do not publish or repeat a record as "fraudulent" based on a score. Doing so can be
  false and defamatory.
- Report suspected fraud through proper channels (e.g. the
  [Washington State Auditor's fraud program](https://sao.wa.gov/), or the relevant
  agency's program-integrity office), which can lawfully access the non-public data
  needed to confirm or clear a lead.
- Respect each dataset's terms of use and applicable privacy law.

## Layout

```
FraudScan/
├── config.json              # sources, rules, thresholds (no code changes needed)
├── fraudscan/
│   ├── cli.py               # ingest / score / run / serve / sources
│   ├── http_util.py         # Socrata (SODA) client — stdlib urllib
│   ├── storage.py           # SQLite schema + helpers
│   ├── scoring.py           # de-correlated severity aggregation + diversity sort
│   ├── taxonomy.py          # rule → family + correlation-group map
│   ├── legitimacy.py        # known-institution suppression
│   ├── external.py          # USAspending + IRS 990 + FAC operator enrichment
│   ├── fac.py               # Federal Audit Clearinghouse single-audit findings (by EIN)
│   ├── crosswalk.py         # sanctioned DOH provider name → NPI + practice city (NLM/NPPES)
│   ├── resolve.py           # cross-source operator clustering (union-find)
│   ├── payments.py          # attach public $ (contracts, multi-year Medicare B/D/DME)
│   ├── screening.py         # OIG LEIE (+DOB/NPI/excl-type) + CMS Revoked + SAM
│   ├── ownership.py         # SNF + hospice + HHA owner networks (PECOS associate id)
│   ├── reassignment.py      # CMS Medicare billing-group reassignment (NPI → group)
│   ├── sos.py               # WA SOS UBI / registered-agent / officer linking
│   ├── context_sources.py   # curated context/coverage loader (data/context/*.csv)
│   ├── doh_discipline.py    # auto-ingest DOH disciplinary-action narratives
│   ├── childcare_enforcement.py  # findchildcarewa per-provider complaints/inspections
│   ├── registry.py          # business-registry CSV loader + name matching
│   ├── sources/             # childcare·contracts·healthcare + cms·nppes (generic) (→ Entity)
│   ├── rules/               # per-source + facility profile + naming + cross.py (→ Flag)
│   └── web/                 # http.server backend + dashboard (queue + operators)
├── examples/                # business_registry_sample.csv (expected registry shape)
├── tests/                   # stdlib unittest, zero deps
└── data/                    # SQLite db + data/registry/*.csv (gitignored)
```

### Enforcement & integrity sources (Phase 1–3 additions)

| Source | What it powers | Refresh |
|---|---|---|
| [WA HCA termination/exclusion list](https://www.hca.wa.gov/billers-providers-partners/become-apple-health-provider/provider-termination-and-exclusion-list) + DSHS list | state Medicaid terminations for cause → screening (matched by license #/NPI — definitive) | cached; `score` re-downloads with screening refresh |
| [NPPES deactivated NPIs](https://download.cms.gov/nppes/NPI_Files.html) | `paid_after_npi_deactivated` — payments dated after NPI deactivation | monthly file, auto-discovered |
| [IRS auto-revocation list](https://www.irs.gov/charities-non-profits/tax-exempt-organization-search-bulk-data-downloads) | operator "501(c) revoked" badge + score bump (EIN join) | checked during `resolve` |
| WA L&I [debar](https://secure.lni.wa.gov/debarandstrike/ContractorDebarList.aspx)/[strike](https://secure.lni.wa.gov/debarandstrike/ContractorStrikeList.aspx) lists | `lni_debarred` / `lni_strike` / `contract_during_debarment` (UBI in evidence) | cached; delete data/cache/lni_debar.json to refresh |
| [OIG Corporate Integrity Agreements](https://oig.hhs.gov/compliance/corporate-integrity-agreements/cia-documents.asp) | operator chip for entities under a CIA — **partial: newest ~20 only** (list endpoint ignores pagination); archive link in every dossier | checked during `resolve` |
| DOJ USAO W.D./E.D. Wash. press feeds | `news-ingest` writes fraud-release matches into curated context (long-name guard) | run `python3 -m fraudscan news-ingest` |
| [CourtListener/RECAP](https://www.courtlistener.com/help/api/rest/) | `courts` — WA federal docket context for ESCALATED/WATCHED leads only; items labeled UNVERIFIED, never scored | run `python3 -m fraudscan courts`; optional COURTLISTENER_TOKEN |

Litigation note: this uses the Free Law Project's public API (not fee-gated PACER), is
restricted to human-escalated leads, and produces context links — not flags.
