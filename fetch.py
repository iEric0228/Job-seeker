"""Source adapters. Pull postings from each enabled source into the jobs table.

Usage:
    python fetch.py                  # every enabled source
    python fetch.py --source adzuna  # just one
    python fetch.py --probe          # print raw response shapes, write nothing

--probe exists because these APIs drift. Run it once on a new machine before
trusting a fetch: it prints the top-level keys and the field names of the first
result from each source so you can eyeball them against the adapter.
"""

import argparse
import html
import json
import re
import sys
from datetime import UTC, date, datetime

import httpx

from db import (
    SOURCE_PRIORITY,
    dedupe_key,
    init_db,
    load_yaml,
    now_iso,
    save_yaml,
)

TIMEOUT = 30
REMOTE_RE = re.compile(r"\bremote\b|\bwork from home\b|\btelework\b", re.IGNORECASE)
ONSITE_RE = re.compile(r"\b(on-?site only|no remote|not remote)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def strip_html(raw):
    """HTML -> readable plain text. Used by every adapter and by enrich.py."""
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


US_STATES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
    "washington dc": "DC",
    "puerto rico": "PR",
}
STATE_CODES = set(US_STATES.values())

# Only the countries these boards actually return in volume.
COUNTRIES = {
    "united states": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "us": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "england": "GB",
    "scotland": "GB",
    "canada": "CA",
    "ireland": "IE",
    "germany": "DE",
    "france": "FR",
    "spain": "ES",
    "netherlands": "NL",
    "poland": "PL",
    "portugal": "PT",
    "india": "IN",
    "singapore": "SG",
    "australia": "AU",
    "japan": "JP",
    "israel": "IL",
    "brazil": "BR",
    "mexico": "MX",
    "china": "CN",
    "switzerland": "CH",
    "sweden": "SE",
    "denmark": "DK",
    "norway": "NO",
    "italy": "IT",
    "belgium": "BE",
    "austria": "AT",
    "romania": "RO",
    "new zealand": "NZ",
    "south africa": "ZA",
    "argentina": "AR",
    "colombia": "CO",
    "philippines": "PH",
    "korea": "KR",
    "taiwan": "TW",
}

# Cities common on ATS boards whose names alone imply a non-US country.
FOREIGN_CITIES = {
    "london": "GB",
    "dublin": "IE",
    "berlin": "DE",
    "munich": "DE",
    "paris": "FR",
    "amsterdam": "NL",
    "madrid": "ES",
    "barcelona": "ES",
    "toronto": "CA",
    "vancouver": "CA",
    "montreal": "CA",
    "bangalore": "IN",
    "bengaluru": "IN",
    "hyderabad": "IN",
    "mumbai": "IN",
    "pune": "IN",
    "singapore": "SG",
    "sydney": "AU",
    "melbourne": "AU",
    "tokyo": "JP",
    "tel aviv": "IL",
    "zurich": "CH",
    "stockholm": "SE",
    "warsaw": "PL",
    "lisbon": "PT",
    "krakow": "PL",
    "sao paulo": "BR",
    "mexico city": "MX",
}


def parse_location(location, default_country=None):
    """Location string -> (country ISO-2, US state code or None).

    Handles "Boston, MA", "Boston, Massachusetts, US", "Remote (US)",
    "London, UK" and bare "London". `default_country` is what to assume when
    nothing in the string identifies a country -- Adzuna is queried per country
    and USAJOBS is federal, so those callers know the answer already.
    """
    text = (location or "").strip().lower()
    if not text:
        return default_country, None

    # Parentheses are separators, not decoration: "Remote (US)" carries the
    # country inside them, and "Boston, MA (Hybrid)" carries noise.
    segments = [s.strip(" ()[]") for s in re.split(r"[,/|()\[\]]| - ", text) if s.strip(" ()[]")]

    # Two-letter codes win over full names, checked across every segment first.
    # "Washington, DC" must not resolve to Washington state on segment one.
    state = None
    for seg in segments:
        if len(seg) == 2 and seg.upper() in STATE_CODES:
            state = seg.upper()
            break
    if state is None:
        for seg in segments:
            if seg in US_STATES:
                state = US_STATES[seg]
                break

    country = None
    for seg in segments:
        if seg in COUNTRIES:
            country = COUNTRIES[seg]
            break
    if country is None:
        for seg in segments:
            if seg in FOREIGN_CITIES:
                country = FOREIGN_CITIES[seg]
                break

    if state and country in (None, "US"):
        return "US", state
    if country and country != "US":
        return country, None  # a US state code cannot apply outside the US
    return country or default_country, state


# "3+ years", "3-5 years", "minimum of 2 years", "at least 18 months"
_YEARS_RE = re.compile(
    r"(?:(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)\s*\d{1,2}|(\d{1,2})\s*(?:\+|plus)?)\s*"
    r"(?:\+\s*)?year",
    re.IGNORECASE,
)
_MONTHS_RE = re.compile(r"(\d{1,2})\s*months?", re.IGNORECASE)

INTERNSHIP_RE = re.compile(r"\b(intern|internship|co-?op|placement student)\b", re.IGNORECASE)
ENTRY_RE = re.compile(
    r"\b(entry.?level|new ?grad(uate)?|junior|jr\.?|associate|trainee|apprentice"
    r"|early career|university grad|campus hire|rotational program)\b",
    re.IGNORECASE,
)
SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|director|head of|manager)\b", re.IGNORECASE
)


def parse_experience(title, description):
    """-> (min_years_required or None, level).

    Years is the *lowest* requirement stated anywhere in the posting. A JD
    saying "3-5 years" wants 3; one saying "2 years of Linux, 5 overall" is
    read as 2. Erring permissive matters here -- wrongly hiding a job you could
    have got is worse than showing one you skip.
    """
    blob = f"{title or ''}\n{description or ''}"

    years = []
    for match in _YEARS_RE.finditer(blob):
        value = match.group(1) or match.group(2)
        if value is not None:
            years.append(int(value))
    for match in _MONTHS_RE.finditer(blob):
        months = int(match.group(1))
        if months >= 6:  # "6 months experience" reads as 0 years, not 6
            years.append(months // 12)
    min_years = min(years) if years else None

    title_text = title or ""
    if INTERNSHIP_RE.search(title_text):
        level = "internship"
    elif ENTRY_RE.search(title_text):
        level = "entry"
    elif SENIOR_RE.search(title_text):
        level = "senior"
    elif INTERNSHIP_RE.search(blob[:2000]):
        level = "internship"
    elif ENTRY_RE.search(blob[:2000]) or min_years is not None and min_years <= 1:
        level = "entry"
    elif min_years is not None and min_years >= 5:
        level = "senior"
    else:
        level = "mid"
    return min_years, level


def guess_remote(title, location, description):
    """0/1/None. Only claims remote when the posting says so somewhere visible."""
    blob = " ".join(filter(None, [title, location]))
    if ONSITE_RE.search(blob):
        return 0
    if REMOTE_RE.search(blob):
        return 1
    head = (description or "")[:1500]
    if REMOTE_RE.search(head) and not ONSITE_RE.search(head):
        return 1
    return None


def to_iso(value):
    """Best-effort timestamp normalisation across the four sources."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # Lever: epoch milliseconds
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")
    text = str(value).strip().replace("Z", "+00:00")
    for parse in (
        lambda s: datetime.fromisoformat(s),
        lambda s: datetime.strptime(s, "%m/%d/%Y"),
        lambda s: datetime.strptime(s, "%Y-%m-%d"),
    ):
        try:
            dt = parse(text)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.isoformat(timespec="seconds")
    return None


def to_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def make_job(source, native_id, **fields):
    """Assemble a normalised row. Every adapter funnels through here."""
    title = fields.get("title") or ""
    company = fields.get("company") or ""
    location = fields.get("location") or ""
    description = fields.get("description") or ""
    remote = fields.get("remote")
    if remote is None:
        remote = guess_remote(title, location, description)
    country, state = parse_location(location, fields.get("default_country"))
    if remote == 1 and country is None:
        country = fields.get("default_country")
    min_years, level = parse_experience(title, description)
    return {
        "id": f"{source}:{native_id}",
        "dedupe_key": dedupe_key(company, title, location),
        "source": source,
        "company": company.strip(),
        "title": title.strip(),
        "location": location.strip(),
        "remote": remote,
        "url": fields.get("url"),
        "description": description,
        "desc_is_full": int(fields.get("desc_is_full", 0)),
        "salary_min": to_int(fields.get("salary_min")),
        "salary_max": to_int(fields.get("salary_max")),
        "posted_at": to_iso(fields.get("posted_at")),
        "first_seen": now_iso(),
        "raw_json": json.dumps(fields.get("raw", {}), default=str)[:200000],
        "country": country,
        "state": state,
        "min_years_exp": min_years,
        "level": level,
    }


# --------------------------------------------------------------------------- #
# persistence + dedup
# --------------------------------------------------------------------------- #

COLUMNS = [
    "id",
    "dedupe_key",
    "source",
    "company",
    "title",
    "location",
    "remote",
    "url",
    "description",
    "desc_is_full",
    "salary_min",
    "salary_max",
    "posted_at",
    "first_seen",
    "raw_json",
    "country",
    "state",
    "min_years_exp",
    "level",
]


def _better(new, existing):
    """True when `new` should replace `existing` for the same dedupe_key.

    ATS board > USAJOBS > Adzuna; within a tier, the longer description wins.
    """
    new_rank = SOURCE_PRIORITY.get(new["source"], 0)
    old_rank = SOURCE_PRIORITY.get(existing["source"], 0)
    if new_rank != old_rank:
        return new_rank > old_rank
    if int(new["desc_is_full"]) != int(existing["desc_is_full"] or 0):
        return int(new["desc_is_full"]) > int(existing["desc_is_full"] or 0)
    return len(new["description"] or "") > len(existing["description"] or "")


def upsert(conn, job):
    """Insert one job, resolving dedupe collisions. Returns 'new'|'replaced'|'skipped'."""
    existing_same_id = conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    if existing_same_id:
        # Refresh volatile fields but keep first_seen and any enriched description.
        keep_desc = existing_same_id["desc_is_full"] and not job["desc_is_full"]
        conn.execute(
            """UPDATE jobs SET title=?, company=?, location=?, remote=?, url=?,
                   description=COALESCE(?, description), salary_min=?, salary_max=?,
                   posted_at=?, raw_json=? WHERE id=?""",
            (
                job["title"],
                job["company"],
                job["location"],
                job["remote"],
                job["url"],
                None if keep_desc else job["description"],
                job["salary_min"],
                job["salary_max"],
                job["posted_at"],
                job["raw_json"],
                job["id"],
            ),
        )
        return "skipped"

    rivals = conn.execute(
        "SELECT * FROM jobs WHERE dedupe_key = ?", (job["dedupe_key"],)
    ).fetchall()
    for rival in rivals:
        # Never discard a row the owner has already acted on.
        tracked = conn.execute(
            "SELECT 1 FROM applications WHERE job_id = ?", (rival["id"],)
        ).fetchone()
        if tracked or not _better(job, rival):
            return "skipped"

    for rival in rivals:
        conn.execute("DELETE FROM scores WHERE job_id = ?", (rival["id"],))
        conn.execute("DELETE FROM jobs WHERE id = ?", (rival["id"],))

    conn.execute(
        f"INSERT INTO jobs ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
        [job[c] for c in COLUMNS],
    )
    return "replaced" if rivals else "new"


# --------------------------------------------------------------------------- #
# adzuna
# --------------------------------------------------------------------------- #


def _quota_left(config):
    quota = config.setdefault("quota", {})
    today = date.today().isoformat()
    if quota.get("adzuna_quota_date") != today:
        quota["adzuna_quota_date"] = today
        quota["adzuna_calls_today"] = 0
    return quota["adzuna_daily_limit"] - quota["adzuna_calls_today"]


def fetch_adzuna(config, resume, probe=False):
    """One search call per target title, highest-weighted first, quota-capped."""
    cfg = config["sources"]["adzuna"]
    if not (cfg.get("app_id") and cfg.get("app_key")):
        print("  adzuna: no credentials in config.yaml, skipping")
        return []

    match = resume.get("match", {})
    search = match.get("search", {})
    titles = sorted(match.get("target_titles", []), key=lambda t: t.get("weight", 0), reverse=True)
    max_titles = int(search.get("max_titles", 6))
    queries = [_pattern_to_query(t["pattern"]) for t in titles[:max_titles]]

    jobs = []
    for query in queries:
        remaining = _quota_left(config)
        if remaining <= 0:
            print(f"  adzuna: daily quota exhausted, {len(queries)} queries planned; stopping")
            break

        url = f"https://api.adzuna.com/v1/api/jobs/{cfg.get('country', 'us')}/search/1"
        params = {
            "app_id": cfg["app_id"],
            "app_key": cfg["app_key"],
            "results_per_page": cfg.get("results_per_page", 50),
            "what": query,
            "where": search.get("where", ""),
            "max_days_old": cfg.get("max_days_old", 7),
            "content-type": "application/json",
        }
        if search.get("distance_km"):
            params["distance"] = search["distance_km"]

        try:
            resp = httpx.get(url, params=params, timeout=TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - one bad query must not kill the run
            print(f"  adzuna: query {query!r} failed: {exc}")
            continue
        finally:
            config["quota"]["adzuna_calls_today"] += 1

        if probe:
            _probe("adzuna", payload, payload.get("results", []))
            return []

        for item in payload.get("results", []):
            jobs.append(
                make_job(
                    "adzuna",
                    item.get("id"),
                    title=item.get("title"),
                    company=(item.get("company") or {}).get("display_name"),
                    location=(item.get("location") or {}).get("display_name"),
                    url=item.get("redirect_url"),
                    # Adzuna excerpts are truncated; enrich.py fills these in later.
                    description=strip_html(item.get("description")),
                    desc_is_full=0,
                    salary_min=item.get("salary_min"),
                    salary_max=item.get("salary_max"),
                    posted_at=item.get("created"),
                    # Adzuna is queried one country at a time, so we know it.
                    default_country=cfg.get("country", "us").upper(),
                    raw=item,
                )
            )
    return jobs


def _pattern_to_query(pattern):
    """Turn a target_titles regex into plain search words Adzuna understands."""
    text = re.sub(r"\\b|\\+|[()\[\]{}?*+^$.]", " ", pattern)
    text = text.split("|")[0]
    return " ".join(text.split())


# --------------------------------------------------------------------------- #
# usajobs
# --------------------------------------------------------------------------- #


def fetch_usajobs(config, resume, probe=False):
    cfg = config["sources"]["usajobs"]
    if not cfg.get("api_key"):
        print("  usajobs: no api key in config.yaml, skipping")
        return []

    email = cfg.get("user_agent") or resume.get("profile", {}).get("email", "")
    if not email:
        print("  usajobs: needs a registered email as User-Agent, skipping")
        return []

    match = resume.get("match", {})
    titles = sorted(match.get("target_titles", []), key=lambda t: t.get("weight", 0), reverse=True)
    max_titles = int(match.get("search", {}).get("max_titles", 6))

    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": email,
        "Authorization-Key": cfg["api_key"],
    }
    jobs = []
    for entry in titles[:max_titles]:
        params = {
            "Keyword": _pattern_to_query(entry["pattern"]),
            "LocationName": match.get("search", {}).get("where", ""),
            "ResultsPerPage": cfg.get("results_per_page", 50),
            "DatePosted": 7,
        }
        try:
            resp = httpx.get(
                "https://data.usajobs.gov/api/search",
                params=params,
                headers=headers,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"  usajobs: query {params['Keyword']!r} failed: {exc}")
            continue

        items = (payload.get("SearchResult") or {}).get("SearchResultItems", [])
        if probe:
            _probe(
                "usajobs",
                payload,
                [i.get("MatchedObjectDescriptor", {}) for i in items],
            )
            return []

        for item in items:
            d = item.get("MatchedObjectDescriptor") or {}
            locations = d.get("PositionLocation") or []
            pay = (d.get("PositionRemuneration") or [{}])[0]
            details = (d.get("UserArea") or {}).get("Details") or {}
            description = "\n\n".join(
                strip_html(part)
                for part in (
                    details.get("JobSummary"),
                    d.get("QualificationSummary"),
                    details.get("MajorDuties"),
                    details.get("Requirements"),
                )
                if part
            )
            jobs.append(
                make_job(
                    "usajobs",
                    item.get("MatchedObjectId") or d.get("PositionID"),
                    title=d.get("PositionTitle"),
                    company=d.get("OrganizationName") or d.get("DepartmentName"),
                    # PositionLocationDisplay is already a clean single string;
                    # PositionLocation[] is the structured fallback.
                    location=d.get("PositionLocationDisplay")
                    or ((locations[0] or {}).get("LocationName") if locations else ""),
                    url=d.get("PositionURI") or d.get("ApplyURI", [None])[0],
                    description=description,
                    desc_is_full=1,
                    salary_min=pay.get("MinimumRange"),
                    salary_max=pay.get("MaximumRange"),
                    default_country="US",  # USAJOBS is federal
                    posted_at=d.get("PublicationStartDate"),
                    raw=d,
                )
            )
    return jobs


# --------------------------------------------------------------------------- #
# greenhouse + lever (public per-company boards, no auth, no quota)
# --------------------------------------------------------------------------- #


def fetch_greenhouse(config, resume, probe=False):
    tokens = load_yaml("companies.yaml").get("greenhouse", []) or []
    jobs = []
    for token in tokens:
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        try:
            resp = httpx.get(
                url, params={"content": "true"}, timeout=TIMEOUT, follow_redirects=True
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"  greenhouse: board {token!r} failed: {exc}")
            continue

        postings = payload.get("jobs", [])
        if probe:
            _probe("greenhouse", payload, postings)
            return []

        for item in postings:
            jobs.append(
                make_job(
                    "greenhouse",
                    f"{token}-{item.get('id')}",
                    title=item.get("title"),
                    company=(item.get("company_name") or token).replace("-", " ").title(),
                    location=(item.get("location") or {}).get("name"),
                    url=item.get("absolute_url"),
                    description=strip_html(item.get("content")),
                    desc_is_full=1,
                    # The board list endpoint does not return first_published --
                    # only the per-job endpoint does. Deliberately NOT falling
                    # back to updated_at: it is a modification date, so a role
                    # reposted today but open for months would score the full
                    # freshness bonus. No date is honest; a wrong one is not.
                    posted_at=item.get("first_published"),
                    raw=item,
                )
            )
    return jobs


def fetch_lever(config, resume, probe=False):
    tokens = load_yaml("companies.yaml").get("lever", []) or []
    jobs = []
    for token in tokens:
        url = f"https://api.lever.co/v0/postings/{token}"
        try:
            resp = httpx.get(url, params={"mode": "json"}, timeout=TIMEOUT, follow_redirects=True)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"  lever: board {token!r} failed: {exc}")
            continue

        postings = payload if isinstance(payload, list) else payload.get("data", [])
        if probe:
            _probe("lever", {"<top level>": "array"}, postings)
            return []

        for item in postings:
            categories = item.get("categories") or {}
            body = item.get("descriptionPlain") or strip_html(item.get("description"))
            extras = "\n\n".join(
                f"{block.get('text', '')}\n{strip_html(block.get('content'))}"
                for block in (item.get("lists") or [])
            )
            tail = item.get("additionalPlain") or strip_html(item.get("additional"))
            jobs.append(
                make_job(
                    "lever",
                    f"{token}-{item.get('id')}",
                    title=item.get("text"),
                    company=token.replace("-", " ").title(),
                    location=categories.get("location"),
                    url=item.get("hostedUrl") or item.get("applyUrl"),
                    description="\n\n".join(filter(None, [body, extras, tail])),
                    desc_is_full=1,
                    remote=1 if categories.get("workplaceType") == "remote" else None,
                    posted_at=item.get("createdAt"),
                    raw=item,
                )
            )
    return jobs


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #

ADAPTERS = {
    "adzuna": fetch_adzuna,
    "usajobs": fetch_usajobs,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
}


def _probe(source, payload, items):
    print(f"\n--- {source} ---")
    keys = list(payload.keys()) if isinstance(payload, dict) else ["<array>"]
    print(f"top-level keys: {keys}")
    if not items:
        print("no results returned; cannot show item shape")
        return
    first = items[0]
    print(f"first item has {len(first)} fields:")
    for key, value in first.items():
        preview = str(value).replace("\n", " ")[:70]
        print(f"  {key:28} {type(value).__name__:6} {preview}")


def run_fetch(sources=None, probe=False, verbose=True):
    """Fetch from each enabled source. Returns (new, replaced, seen)."""
    config = load_yaml("config.yaml")
    resume = load_yaml("resume.yaml")
    conn = init_db()

    selected = sources or [
        name for name, cfg in config.get("sources", {}).items() if cfg.get("enabled")
    ]
    counts = {"new": 0, "replaced": 0, "skipped": 0}
    for name in selected:
        if name not in ADAPTERS:
            print(f"unknown source {name!r}")
            continue
        if verbose:
            print(f"fetching {name}...")
        jobs = ADAPTERS[name](config, resume, probe=probe)
        if probe:
            continue
        for job in jobs:
            counts[upsert(conn, job)] += 1
        if verbose:
            print(f"  {name}: {len(jobs)} postings returned")

    if probe:
        # A probe still spends real Adzuna calls, so the counter has to persist
        # even though nothing was written to the database.
        save_yaml("config.yaml", config)
    else:
        conn.commit()
        config.setdefault("state", {})["last_fetch"] = now_iso()
        save_yaml("config.yaml", config)
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if verbose:
            print(
                f"\n{counts['new']} new, {counts['replaced']} replaced a duplicate, "
                f"{counts['skipped']} already known -> {total} jobs total"
            )
    conn.close()
    return counts["new"], counts["replaced"], counts["skipped"]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--source",
        action="append",
        choices=sorted(ADAPTERS),
        help="limit to one source",
    )
    ap.add_argument("--probe", action="store_true", help="print raw response shapes, write nothing")
    args = ap.parse_args()
    run_fetch(sources=args.source, probe=args.probe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
