"""Deterministic scoring. No LLM, no embeddings -- every point is traceable.

    score = (title_score x 3) + keyword_score + location_score
            + freshness_score - penalty

Run `python score.py` after any edit to resume.yaml; it rescores everything.
"""

import argparse
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime

from db import connect, init_db, load_yaml, now_iso

KEYWORD_CAP = 30
GAP_PENALTY_EACH = 2
GAP_PENALTY_CAP = 6
SALARY_PENALTY = 5
FRESH_48H = 3
FRESH_7D = 1

FORMULA = "(title x 3) + keywords(<=30) + location + freshness - penalty"


# --------------------------------------------------------------------------- #
# profile compilation
# --------------------------------------------------------------------------- #


def _rx(pattern):
    return re.compile(pattern, re.IGNORECASE)


def _term_rx(term):
    """Literal keyword -> word-boundary regex. Survives 'c++', 'ci/cd', '.net'."""
    body = re.escape(str(term))
    prefix = r"\b" if str(term)[:1].isalnum() else ""
    suffix = r"\b" if str(term)[-1:].isalnum() else ""
    return re.compile(prefix + body + suffix, re.IGNORECASE)


def compile_profile(resume):
    """Pre-compile every pattern in resume.yaml once per run."""
    match = (resume or {}).get("match", {}) or {}
    keywords = match.get("keywords", {}) or {}

    weighted = []
    for bucket in ("strong", "nice"):
        for term, weight in (keywords.get(bucket) or {}).items():
            weighted.append((str(term), int(weight), _term_rx(term)))

    return {
        "target_titles": [
            (_rx(entry["pattern"]), int(entry.get("weight", 0)))
            for entry in match.get("target_titles", []) or []
        ],
        "exclude_titles": [_rx(p) for p in match.get("exclude_titles", []) or []],
        "exclude_description": [_rx(p) for p in match.get("exclude_description", []) or []],
        "keywords": weighted,
        "gaps": [(str(t), _term_rx(t)) for t in (keywords.get("gaps") or [])],
        "locations": [
            (_rx(entry["pattern"]), int(entry.get("weight", 0)))
            for entry in match.get("locations", []) or []
        ],
        "salary_floor": match.get("salary_floor"),
        "max_years_experience": match.get("max_years_experience"),
        "allowed_levels": set(match.get("levels") or []),
        "allowed_countries": set(match.get("countries") or []),
    }


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #


def hours_since(timestamp):
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(str(timestamp))  # 3.11+ parses a trailing Z
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds() / 3600


def score_job(job, profile):
    """Score one job dict.

    Returns a dict of components, or {'dropped': <reason>} when a hard filter
    rejects it. `job` needs: title, description, location, remote, salary_max,
    posted_at.
    """
    title = job.get("title") or ""
    description = job.get("description") or ""
    location = job.get("location") or ""

    # Order matters for diagnosis, not for outcome. Every filter here is ANDed,
    # so the surviving set is identical whichever order they run in -- but
    # whichever fires FIRST is the reason --why reports. Target-title matching
    # goes first because it is by far the most selective: an ATS board is mostly
    # sales, design and finance roles. Without this ordering a "Senior Marketing
    # Manager" is blamed on the seniority rule, which makes that rule look
    # expensive and hides the fact that it was never a candidate at all.
    title_score = max(
        (weight for pattern, weight in profile["target_titles"] if pattern.search(title)),
        default=None,
    )
    if title_score is None:
        return {"dropped": "no_title_match"}

    # From here down, every drop is a job that WAS one of your target roles.
    for pattern in profile["exclude_titles"]:
        if pattern.search(title):
            return {"dropped": f"exclude_title:{pattern.pattern}"}
    for pattern in profile["exclude_description"]:
        if pattern.search(description):
            return {"dropped": f"exclude_description:{pattern.pattern}"}

    # Experience and geography are hard filters, not score components: wanting
    # entry-level work means a 7-years-required job is not a weaker match, it
    # is the wrong job. Jobs that state no requirement are kept.
    max_years = profile["max_years_experience"]
    min_years = job.get("min_years_exp")
    if max_years is not None and min_years is not None and min_years > max_years:
        return {"dropped": f"needs_{min_years}y_experience"}

    allowed_levels = profile["allowed_levels"]
    if allowed_levels and job.get("level") and job["level"] not in allowed_levels:
        return {"dropped": f"level:{job['level']}"}

    allowed_countries = profile["allowed_countries"]
    if allowed_countries and job.get("country") and job["country"] not in allowed_countries:
        return {"dropped": f"country:{job['country']}"}

    haystack = f"{title}\n{description}"
    keyword_score = 0
    matched = []
    for term, weight, pattern in profile["keywords"]:
        if pattern.search(haystack):
            matched.append(term)
            keyword_score += weight
    keyword_score = min(keyword_score, KEYWORD_CAP)

    # A remote posting satisfies the "Remote (US)" location entry.
    location_haystack = location + (" Remote (US)" if job.get("remote") == 1 else "")
    location_score = max(
        (weight for pattern, weight in profile["locations"] if pattern.search(location_haystack)),
        default=0,
    )

    age = hours_since(job.get("posted_at"))
    if age is None:
        freshness_score = 0
    elif age < 48:
        freshness_score = FRESH_48H
    elif age < 24 * 7:
        freshness_score = FRESH_7D
    else:
        freshness_score = 0

    # 3. penalties
    penalty = 0
    floor = profile["salary_floor"]
    salary_max = job.get("salary_max")
    if floor and salary_max is not None and salary_max < floor:
        penalty += SALARY_PENALTY

    gap_flags = [term for term, pattern in profile["gaps"] if pattern.search(haystack)]
    penalty += min(len(gap_flags) * GAP_PENALTY_EACH, GAP_PENALTY_CAP)

    total = (title_score * 3) + keyword_score + location_score + freshness_score - penalty
    return {
        "score": total,
        "title_score": title_score,
        "keyword_score": keyword_score,
        "location_score": location_score,
        "freshness_score": freshness_score,
        "penalty": penalty,
        "matched_keywords": matched,
        "gap_flags": gap_flags,
    }


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def run_score(verbose=True, explain=False):
    """Rescore every job. Returns (scored, dropped)."""
    resume = load_yaml("resume.yaml")
    profile = compile_profile(resume)
    conn = init_db()

    applied = {r["job_id"] for r in conn.execute("SELECT job_id FROM applications")}
    rows = conn.execute("SELECT * FROM jobs").fetchall()

    scored = dropped = 0
    reasons = Counter()
    stamp = now_iso()
    for row in rows:
        job = dict(row)
        if job["id"] in applied:
            conn.execute("DELETE FROM scores WHERE job_id = ?", (job["id"],))
            dropped += 1
            reasons["already in applications"] += 1
            continue
        result = score_job(job, profile)
        if "dropped" in result:
            conn.execute("DELETE FROM scores WHERE job_id = ?", (job["id"],))
            dropped += 1
            reasons[result["dropped"]] += 1
            continue
        conn.execute(
            """INSERT INTO scores (job_id, score, title_score, keyword_score,
                   location_score, freshness_score, penalty, matched_keywords,
                   gap_flags, scored_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                   score=excluded.score, title_score=excluded.title_score,
                   keyword_score=excluded.keyword_score,
                   location_score=excluded.location_score,
                   freshness_score=excluded.freshness_score,
                   penalty=excluded.penalty,
                   matched_keywords=excluded.matched_keywords,
                   gap_flags=excluded.gap_flags, scored_at=excluded.scored_at""",
            (
                job["id"],
                result["score"],
                result["title_score"],
                result["keyword_score"],
                result["location_score"],
                result["freshness_score"],
                result["penalty"],
                json.dumps(result["matched_keywords"]),
                json.dumps(result["gap_flags"]),
                stamp,
            ),
        )
        scored += 1

    conn.commit()
    if verbose:
        print(f"scored {scored}, dropped {dropped} of {len(rows)} jobs")
        if explain and reasons:
            print("\nwhy jobs were dropped:")
            for reason, count in reasons.most_common():
                print(f"  {count:>5}  {reason}")
            print("\nEach line is a rule in resume.yaml. If one is eating everything,")
            print("that is the rule to loosen.")
    conn.close()
    return scored, dropped


def show_top(limit=20):
    conn = connect()
    rows = conn.execute(
        """SELECT s.score, s.title_score, s.keyword_score, s.location_score,
                  s.freshness_score, s.penalty, j.title, j.company, j.location,
                  j.level, j.min_years_exp, j.country, j.state
           FROM scores s JOIN jobs j ON j.id = s.job_id
           ORDER BY s.score DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    print(f"\ntop {len(rows)}   [{FORMULA}]")
    print(
        f"{'tot':>4} {'ttl':>4} {'kw':>4} {'loc':>4} {'fr':>3} {'pen':>4} "
        f"{'level':<11}{'yrs':>4} {'geo':<7} title / company"
    )
    for r in rows:
        geo = "/".join(x for x in (r["country"], r["state"]) if x)
        print(
            f"{r['score']:>4} {r['title_score']:>4} {r['keyword_score']:>4} "
            f"{r['location_score']:>4} {r['freshness_score']:>3} {r['penalty']:>4} "
            f"{(r['level'] or '?'):<11}{(r['min_years_exp'] if r['min_years_exp'] is not None else '-'):>4} "
            f"{geo:<7} {(r['title'] or '')[:36]:36} {(r['company'] or '')[:18]}"
        )
    # The dashboard hides anything under its Min score slider, default 40.
    below = sum(1 for r in rows if r["score"] < 40)
    if below:
        print(
            f"\n{below} of these score under 40 and are hidden by the dashboard's default slider."
        )
    conn.close()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--top", type=int, metavar="N", help="print the top N after scoring")
    ap.add_argument(
        "--why",
        action="store_true",
        help="break down which rule dropped each job -- run this when the list looks empty",
    )
    args = ap.parse_args()
    run_score(explain=args.why)
    if args.top:
        show_top(args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
