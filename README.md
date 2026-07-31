# Job Match Engine

Ten ranked job matches every morning, applied to by hand in under 45 minutes.

Personal tool, single user, runs on a laptop. No cloud, no auto-apply, no LLM in
the scoring path — every point a job scores is traceable to a line in
`resume.yaml`.

## Setup

```bash
uv sync
python db.py --init
```

Then put your API keys in `config.yaml`:

| Source | Where | Notes |
|---|---|---|
| Adzuna | <https://developer.adzuna.com/> | free `app_id` + `app_key`, ~1,000 calls/mo |
| USAJOBS | <https://developer.usajobs.gov/> | free key; `User-Agent` must be the email you registered |
| Greenhouse / Lever | none | public per-company boards, listed in `companies.yaml` |

Keep credentials out of git once they're real:

```bash
git update-index --skip-worktree config.yaml
```

## Daily use

```bash
python fetch.py     # pull new postings  (Adzuna capped at 10 calls/day)
python score.py     # rank them
python enrich.py    # full descriptions for the top 25
python score.py     # rescore: the full JD changes keyword_score a lot
streamlit run app.py
```

The second `score.py` is not a typo. `enrich.py` replaces Adzuna's truncated
excerpt with the full posting, and scoring before that means ranking on text you
no longer have. On a realistic listing it is worth around 20 points.

Or just open the dashboard and press **Fetch now**, which runs the whole
sequence inline.

A cron line, if you want it waiting for you:

```
0 7 * * * cd /path/to/job-match && python fetch.py && python score.py && python enrich.py && python score.py
```

## Verify the APIs before trusting a run

These endpoints drift. `--probe` prints the top-level keys and every field of the
first result, per source, and writes nothing:

```bash
python fetch.py --probe
python fetch.py --source greenhouse --probe
```

If the field names don't match what the adapter reads, fix the adapter — don't
guess.

## Tuning the scorer

```
score = (title_score x 3) + keyword_score + location_score + freshness_score - penalty
```

| Component | Range | Source |
|---|---|---|
| `title_score` | 0–10, tripled | best-matching `match.target_titles` pattern; **no match drops the job** |
| `keyword_score` | 0–30 | sum of `keywords.strong` + `.nice` weights, each term counted once |
| `location_score` | 0–10 | best-matching `match.locations` entry; `remote=1` satisfies "Remote (US)" |
| `freshness_score` | 0/1/3 | posted <48h → 3, <7d → 1 |
| `penalty` | 0–11 | `salary_max` below `salary_floor` → 5; each `keywords.gaps` term → 2, capped at 6 |

Everything lives in `resume.yaml`. Edit it, re-run `python score.py`, and use the
**Tuning** tab to see what changed — it shows every component in its own column,
plus the most frequent terms among *dropped* jobs, which is how you catch
`exclude_titles` eating roles you actually wanted.

`target_titles`, `exclude_titles`, `exclude_description` and `locations` are
regex. `keywords` are literal terms matched on word boundaries, so `c++` and
`ci/cd` work as written.

Note that title and keywords have the same 30-point ceiling, so a lower-tier
title with a very dense JD can outrank a top-tier title with a thin one. If the
daily ten start drifting off-title, lower `KEYWORD_CAP` in `score.py`.

## Layout

```
db.py          schema + connection helpers
fetch.py       Adzuna · USAJOBS · Greenhouse · Lever adapters, dedup, quota
score.py       jobs -> scores
enrich.py      full JD via JSON-LD, 1 req/sec, robots.txt honoured
app.py         Streamlit dashboard (Today's Ten · Pipeline · Tuning)
test_score.py  the only tests
resume.yaml    match profile — the file you actually tune
companies.yaml ATS board tokens
config.yaml    keys, source toggles, quota state
data/jobs.db   SQLite
```

Duplicates are collapsed on `sha1(company|normalised_title|city)`, keeping the
row with the fuller description: ATS board > USAJOBS > Adzuna. A job you've
already acted on is never displaced.

## Tests

```bash
pytest test_score.py
ruff check . && ruff format .
```

The scorer is the only thing under test, by design.
