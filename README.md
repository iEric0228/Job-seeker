# Job Match Engine

Ten ranked job matches every morning, applied to by hand in under 45 minutes.

Personal tool, single user, runs on a laptop. No cloud, no auto-apply, no LLM in
the scoring path — every point a job scores is traceable to a line in
`resume.yaml`.

## Setup

Needs **Python 3.11+** (the code uses `datetime.UTC`). You do not have to install
it yourself — `uv` reads `requires-python` from `pyproject.toml` and fetches a
suitable interpreter.

```bash
uv sync
uv run python db.py --init
```

**Run everything through `uv run`.** A bare `python` or `python3` picks up
whatever the system ships — on macOS that is often Xcode's Python 3.9, which
fails with `ImportError: cannot import name 'UTC' from 'datetime'`. `uv run`
always uses the project's interpreter.

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
uv run python fetch.py     # pull new postings  (Adzuna capped at 10 calls/day)
uv run python score.py     # rank them
uv run python enrich.py    # full descriptions for the top 25
uv run python score.py     # rescore: the full JD changes keyword_score a lot
uv run streamlit run app.py
```

The second `score.py` is not a typo. `enrich.py` replaces Adzuna's truncated
excerpt with the full posting, and scoring before that means ranking on text you
no longer have. On a realistic listing it is worth around 20 points.

Or just open the dashboard and press **Fetch now**, which runs the whole
sequence inline.

A cron line, if you want it waiting for you:

```
0 7 * * * cd /path/to/job-match && /full/path/to/uv run python fetch.py && /full/path/to/uv run python score.py && /full/path/to/uv run python enrich.py && /full/path/to/uv run python score.py
```

cron runs with a minimal `PATH`, so give `uv` its absolute path — `which uv`
will tell you (typically `~/.local/bin/uv` or `/opt/homebrew/bin/uv`).

## Verify the APIs before trusting a run

These endpoints drift. `--probe` prints the top-level keys and every field of the
first result, per source, and writes nothing:

```bash
uv run python fetch.py --probe
uv run python fetch.py --source greenhouse --probe
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
uv run pytest test_score.py
uv run ruff check . && uv run ruff format .
```

The scorer is the only thing under test, by design.
