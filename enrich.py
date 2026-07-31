"""Fetch full job descriptions via JSON-LD for the daily top N.

Aggregators truncate descriptions -- fine for ranking, useless for deciding.
This fetches the posting page the owner is about to open anyway and pulls the
schema.org JobPosting block out of it.

Politeness, non-negotiable: 1 request/second, descriptive User-Agent,
robots.txt honoured, at most two retries, and any failure falls back to the
excerpt already in the database. This never crashes the pipeline.
"""

import argparse
import json
import sys
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from db import connect, load_yaml
from fetch import strip_html, to_int

_ROBOTS = {}


def robots_allows(url, user_agent):
    """True unless robots.txt explicitly disallows. Unreachable robots => allowed."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _ROBOTS:
        parser = urllib.robotparser.RobotFileParser()
        try:
            resp = httpx.get(
                urljoin(origin, "/robots.txt"),
                timeout=10,
                headers={"User-Agent": user_agent},
                follow_redirects=True,
            )
            parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except Exception:  # noqa: BLE001 - no robots.txt is not a reason to stop
            parser.parse([])
        _ROBOTS[origin] = parser
    try:
        return _ROBOTS[origin].can_fetch(user_agent, url)
    except Exception:  # noqa: BLE001
        return True


def iter_json_ld(html_text):
    """Yield every JSON-LD object on the page, flattening arrays and @graph."""
    soup = BeautifulSoup(html_text, "lxml")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "@graph" in node:
                    stack.extend(
                        node["@graph"] if isinstance(node["@graph"], list) else [node["@graph"]]
                    )
                yield node


def find_job_posting(html_text):
    for node in iter_json_ld(html_text):
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if any(str(t).lower() == "jobposting" for t in types):
            return node
    return None


def _salary(posting):
    """schema.org baseSalary -> (min, max). Shapes vary wildly; be forgiving."""
    base = posting.get("baseSalary") or {}
    if isinstance(base, list):
        base = base[0] if base else {}
    value = base.get("value") if isinstance(base, dict) else None
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, dict):
        return to_int(value), to_int(value)

    lo = to_int(value.get("minValue"))
    hi = to_int(value.get("maxValue"))
    if lo is None and hi is None:
        exact = to_int(value.get("value"))
        lo = hi = exact
    # Hourly and monthly rates would poison the salary-floor penalty.
    unit = str(value.get("unitText") or "").upper()
    if unit in {"HOUR", "HOURLY"}:
        lo, hi = (v * 2080 if v else None for v in (lo, hi))
    elif unit in {"MONTH", "MONTHLY"}:
        lo, hi = (v * 12 if v else None for v in (lo, hi))
    elif unit in {"WEEK", "WEEKLY"}:
        lo, hi = (v * 52 if v else None for v in (lo, hi))
    return lo, hi


def _location(posting):
    loc = posting.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return None
    address = loc.get("address")
    if isinstance(address, list):
        address = address[0] if address else None
    if not isinstance(address, dict):
        return None
    parts = [
        address.get("addressLocality"),
        address.get("addressRegion"),
        address.get("addressCountry")
        if isinstance(address.get("addressCountry"), str)
        else (address.get("addressCountry") or {}).get("name"),
    ]
    return ", ".join(p for p in parts if p) or None


def parse_posting(html_text):
    """HTML -> dict of enriched fields, or None when there is no JobPosting."""
    posting = find_job_posting(html_text)
    if not posting:
        return None
    description = strip_html(posting.get("description"))
    if not description:
        return None
    salary_min, salary_max = _salary(posting)
    employment = posting.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(e) for e in employment)
    return {
        "description": description,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "location": _location(posting),
        "employment_type": employment,
    }


def fetch_page(url, user_agent, timeout, max_retries):
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"}
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (403, 404, 410):
                return None  # not a transient failure; do not retry
        except Exception:  # noqa: BLE001, S110 - a courtesy fetch never breaks the run
            pass
        if attempt < max_retries:
            time.sleep(2**attempt)
    return None


def run_enrich(top_n=None, verbose=True):
    """Enrich the highest-scoring not-yet-full jobs. Returns (enriched, attempted)."""
    config = load_yaml("config.yaml")
    cfg = config.get("enrich", {}) or {}
    top_n = top_n or int(cfg.get("top_n", 25))
    user_agent = cfg.get("user_agent", "job-match-engine/1.0")
    delay = float(cfg.get("request_delay_seconds", 1.0))
    timeout = float(cfg.get("timeout_seconds", 20))
    retries = int(cfg.get("max_retries", 2))

    conn = connect()
    targets = conn.execute(
        """SELECT j.id, j.url, j.title FROM jobs j
           JOIN scores s ON s.job_id = j.id
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE a.job_id IS NULL AND j.desc_is_full = 0 AND j.url IS NOT NULL
           ORDER BY s.score DESC LIMIT ?""",
        (top_n,),
    ).fetchall()

    enriched = 0
    for i, row in enumerate(targets):
        if i:
            time.sleep(delay)  # 1 req/sec, always
        if not robots_allows(row["url"], user_agent):
            if verbose:
                print(f"  robots.txt disallows {row['url']}")
            continue

        html_text = fetch_page(row["url"], user_agent, timeout, retries)
        if not html_text:
            continue
        parsed = parse_posting(html_text)
        if not parsed:
            continue  # keep the excerpt

        conn.execute(
            """UPDATE jobs SET description = ?, desc_is_full = 1,
                   salary_min = COALESCE(?, salary_min),
                   salary_max = COALESCE(?, salary_max),
                   location = COALESCE(?, location)
               WHERE id = ?""",
            (
                parsed["description"],
                parsed["salary_min"],
                parsed["salary_max"],
                parsed["location"],
                row["id"],
            ),
        )
        enriched += 1
        if verbose:
            print(f"  + {row['title'][:60]}")

    conn.commit()
    conn.close()
    if verbose:
        print(f"enriched {enriched} of {len(targets)} attempted")
    return enriched, len(targets)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--top", type=int, help="how many jobs to enrich (default: config.yaml)")
    args = ap.parse_args()
    run_enrich(top_n=args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
