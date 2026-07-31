"""Streamlit dashboard. Ten decisions in under 45 minutes.

streamlit run app.py
"""

import json
import re
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta

import pandas as pd
import streamlit as st

import enrich as enriching
import fetch
import score as scoring
from db import init_db, load_yaml, now_iso

DAILY_GOAL = 10
QUEUE_SIZE = 10
BENCH_SIZE = 60
STATUSES = ["applied", "skipped", "interviewing", "rejected", "offer"]
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "our",
    "you",
    "are",
    "will",
    "this",
    "that",
    "have",
    "has",
    "not",
    "job",
    "work",
    "team",
    "new",
    "all",
    "your",
    "who",
    "inc",
    "llc",
    "of",
    "in",
    "to",
    "a",
    "an",
    "at",
    "on",
    "is",
    "we",
    "or",
}


# --------------------------------------------------------------------------- #
# data access -- kept free of streamlit calls so it stays testable
# --------------------------------------------------------------------------- #


def candidate_jobs(conn, min_score=40, sources=None, remote_only=False, days=7, limit=BENCH_SIZE):
    """Ranked, deduped, excluding anything already in applications."""
    sql = """
        SELECT j.*, s.score, s.title_score, s.keyword_score, s.location_score,
               s.freshness_score, s.penalty, s.matched_keywords, s.gap_flags
        FROM scores s
        JOIN jobs j ON j.id = s.job_id
        LEFT JOIN applications a ON a.job_id = j.id
        WHERE a.job_id IS NULL AND s.score >= ?
    """
    params = [min_score]
    if sources:
        sql += f" AND j.source IN ({','.join('?' * len(sources))})"
        params.extend(sources)
    if remote_only:
        sql += " AND j.remote = 1"
    if days:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        sql += " AND (j.posted_at IS NULL OR j.posted_at >= ?)"
        params.append(cutoff)
    sql += " ORDER BY s.score DESC, j.posted_at DESC LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in conn.execute(sql, params)]
    for row in rows:
        row["matched_keywords"] = json.loads(row["matched_keywords"] or "[]")
        row["gap_flags"] = json.loads(row["gap_flags"] or "[]")
    return rows


def mark(conn, job_id, status, notes=None):
    conn.execute(
        """INSERT INTO applications (job_id, status, applied_at, notes)
           VALUES (?,?,?,?)
           ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,
               applied_at=excluded.applied_at, notes=COALESCE(excluded.notes, notes)""",
        (job_id, status, now_iso(), notes),
    )
    conn.commit()


def local_day_bounds(day):
    """UTC bounds of a local calendar day.

    applied_at is stored in UTC, but "applied today" has to mean the day on the
    owner's wall clock -- otherwise an 8pm Boston application lands on tomorrow's
    UTC date and drops out of the counter.
    """
    start = datetime.combine(day, time.min).astimezone()
    end = start + timedelta(days=1)
    return (
        start.astimezone(UTC).isoformat(timespec="seconds"),
        end.astimezone(UTC).isoformat(timespec="seconds"),
    )


def applied_today(conn):
    start, end = local_day_bounds(date.today())
    return conn.execute(
        """SELECT COUNT(*) FROM applications
           WHERE status != 'skipped' AND applied_at >= ? AND applied_at < ?""",
        (start, end),
    ).fetchone()[0]


def pipeline_metrics(conn):
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    applied = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status != 'skipped'"
    ).fetchone()[0]
    this_week = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status != 'skipped' AND applied_at >= ?",
        (week_ago,),
    ).fetchone()[0]
    tracked = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    replies = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status IN ('interviewing','offer')"
    ).fetchone()[0]
    return {
        "applied_today": applied_today(conn),
        "applied_week": this_week,
        "tracked": tracked,
        "response_rate": (replies / applied * 100) if applied else 0.0,
    }


def applications_per_day(conn, days=14):
    # Bucket in local time for the same reason applied_today() does.
    counts = Counter()
    for (stamp,) in conn.execute(
        """SELECT applied_at FROM applications
           WHERE status != 'skipped' AND applied_at IS NOT NULL"""
    ):
        try:
            counts[datetime.fromisoformat(stamp).astimezone().date().isoformat()] += 1
        except ValueError:
            continue
    today = date.today()
    index = [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]
    return pd.DataFrame({"applied": [counts.get(d, 0) for d in index]}, index=index)


def tuning_rows(conn, limit=100):
    rows = conn.execute(
        """SELECT j.title, j.company, j.source, s.score AS total,
                  s.title_score AS title_pts, s.keyword_score AS kw_pts,
                  s.location_score AS loc_pts, s.freshness_score AS fresh_pts,
                  s.penalty AS penalty_pts, s.matched_keywords
           FROM scores s JOIN jobs j ON j.id = s.job_id
           ORDER BY s.scored_at DESC, s.score DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["matched_keywords"] = ", ".join(json.loads(d.pop("matched_keywords") or "[]"))
        out.append(d)
    return pd.DataFrame(out)


def keyword_frequency(conn, limit=20):
    counter = Counter()
    for (blob,) in conn.execute("SELECT matched_keywords FROM scores"):
        counter.update(json.loads(blob or "[]"))
    return pd.DataFrame(counter.most_common(limit), columns=["keyword", "jobs"])


def dropped_terms(conn, limit=20):
    """Words common in jobs that never made it into `scores`.

    This is how you notice exclude_titles is eating roles you wanted.
    """
    counter = Counter()
    dropped = 0
    for (title,) in conn.execute(
        """SELECT j.title FROM jobs j
           LEFT JOIN scores s ON s.job_id = j.id
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE s.job_id IS NULL AND a.job_id IS NULL"""
    ):
        dropped += 1
        counter.update(
            w for w in re.findall(r"[a-z0-9+#/]{3,}", (title or "").lower()) if w not in STOPWORDS
        )
    df = pd.DataFrame(counter.most_common(limit), columns=["term", "dropped jobs"])
    return df, dropped


# --------------------------------------------------------------------------- #
# rendering helpers
# --------------------------------------------------------------------------- #


def badge_color(value):
    if value >= 70:
        return "#1a7f37"
    if value >= 50:
        return "#bf8700"
    return "#6e7781"


def money(row):
    lo, hi = row.get("salary_min"), row.get("salary_max")
    if not lo and not hi:
        return ""
    fmt = lambda v: f"${v / 1000:.0f}k" if v else "?"
    return f"{fmt(lo)}–{fmt(hi)}" if lo and hi else fmt(lo or hi)


def age_text(posted_at):
    hours = scoring.hours_since(posted_at)
    if hours is None:
        return "date unknown"
    if hours < 24:
        return "today"
    days = int(hours // 24)
    return f"{days} day{'s' if days > 1 else ''} ago"


def card_html(job):
    chips = "".join(
        f'<span style="background:#dafbe1;color:#1a7f37;border-radius:10px;'
        f'padding:1px 8px;margin-right:4px;font-size:12px;white-space:nowrap">✅ {k}</span>'
        for k in job["matched_keywords"][:10]
    )
    gaps = "".join(
        f'<span style="background:#fff8c5;color:#7d4e00;border-radius:10px;'
        f'padding:1px 8px;margin-right:4px;font-size:12px;white-space:nowrap">⚠️ {k}</span>'
        for k in job["gap_flags"][:6]
    )
    remote = " (Remote)" if job.get("remote") == 1 else ""
    salary = money(job)
    return f"""
<div style="display:flex;gap:14px;align-items:flex-start">
  <div style="background:{badge_color(job["score"])};color:#fff;border-radius:8px;
              min-width:58px;height:58px;display:flex;align-items:center;
              justify-content:center;font-size:26px;font-weight:700">{job["score"]}</div>
  <div style="flex:1;min-width:0">
    <div style="display:flex;justify-content:space-between;gap:12px">
      <span style="font-size:17px;font-weight:600">{job["title"] or "Untitled"}</span>
      <span style="font-size:15px;color:#57606a;white-space:nowrap">{salary}</span>
    </div>
    <div style="color:#57606a;font-size:13px;margin:2px 0 6px 0">
      {job["company"] or "Unknown"} · {job["location"] or "Location unknown"}{remote}
      · {age_text(job["posted_at"])} · {job["source"]}
    </div>
    <div style="line-height:1.9">{chips}{gaps}</div>
  </div>
</div>"""


# --------------------------------------------------------------------------- #
# queue state
# --------------------------------------------------------------------------- #


def filter_signature(filters):
    return json.dumps(filters, sort_keys=True, default=str)


def rebuild_queue(conn, filters):
    pool = candidate_jobs(conn, **filters)
    st.session_state.queue = pool[:QUEUE_SIZE]
    st.session_state.bench = pool[QUEUE_SIZE:]
    st.session_state.filter_sig = filter_signature(filters)


def resolve(conn, job_id, status):
    """Record a decision, drop the card, slide the next-ranked job in."""
    mark(conn, job_id, status)
    st.session_state.queue = [j for j in st.session_state.queue if j["id"] != job_id]
    while st.session_state.bench and len(st.session_state.queue) < QUEUE_SIZE:
        st.session_state.queue.append(st.session_state.bench.pop(0))


# --------------------------------------------------------------------------- #
# ui
# --------------------------------------------------------------------------- #


def main():
    st.set_page_config(page_title="Job Match Engine", page_icon="🎯", layout="wide")
    conn = init_db()
    config = load_yaml("config.yaml")

    with st.sidebar:
        st.header("🎯 Job Match")

        if st.button("Fetch now", type="primary", use_container_width=True):
            with st.spinner("Fetching sources…"):
                new, _, _ = fetch.run_fetch(verbose=False)
            with st.spinner("Scoring…"):
                scored, _ = scoring.run_score(verbose=False)
            # Enrich the top jobs, then score again: enrichment replaces a
            # truncated excerpt with the full JD, which moves keyword_score a
            # lot. Ranking on the pre-enrichment text would waste the fetch.
            with st.spinner("Fetching full descriptions (~1/sec)…"):
                enriched, _ = enriching.run_enrich(verbose=False)
            if enriched:
                with st.spinner("Rescoring enriched jobs…"):
                    scored, _ = scoring.run_score(verbose=False)
            st.success(f"{new} new jobs, {scored} scored, {enriched} enriched")
            st.session_state.pop("filter_sig", None)  # force a queue rebuild

        last = (config.get("state") or {}).get("last_fetch") or "never"
        st.caption(f"Last fetch: {last}")

        st.divider()
        min_score = st.slider("Min score", 0, 80, 40)
        available = [
            r[0] for r in conn.execute("SELECT DISTINCT source FROM jobs ORDER BY 1").fetchall()
        ]
        sources = st.multiselect("Sources", available, default=available)
        remote_only = st.toggle("Remote only")
        window = st.selectbox(
            "Posted within", [1, 3, 7, 14, 30], index=2, format_func=lambda d: f"{d}d"
        )

        st.divider()
        done = applied_today(conn)
        st.metric("Applied today", f"{done} / {DAILY_GOAL}")
        st.progress(min(done / DAILY_GOAL, 1.0))

    filters = {
        "min_score": min_score,
        "sources": sources,
        "remote_only": remote_only,
        "days": window,
    }
    if st.session_state.get("filter_sig") != filter_signature(filters):
        rebuild_queue(conn, filters)

    tab1, tab2, tab3 = st.tabs(["Today's Ten", "Pipeline", "Tuning"])
    with tab1:
        render_today(conn)
    with tab2:
        render_pipeline(conn)
    with tab3:
        render_tuning(conn)


@st.fragment
def render_today(conn):
    """Fragment so apply/skip reruns this list only, not the whole script."""
    queue = st.session_state.queue
    if not queue:
        st.info(
            "Nothing left in the queue. Lower the min score, widen the date "
            "window, or hit **Fetch now**."
        )
        return

    st.caption(f"{len(queue)} shown · {len(st.session_state.bench)} waiting behind them")
    for job in list(queue):
        with st.container(border=True):
            st.markdown(card_html(job), unsafe_allow_html=True)
            left, mid, right, spacer = st.columns([1.1, 1.1, 1, 5])
            left.link_button("Open ↗", job["url"] or "#", use_container_width=True)
            if mid.button("Applied ✓", key=f"a_{job['id']}", use_container_width=True):
                resolve(conn, job["id"], "applied")
                st.rerun(scope="fragment")
            if right.button("Skip ✗", key=f"s_{job['id']}", use_container_width=True):
                resolve(conn, job["id"], "skipped")
                st.rerun(scope="fragment")
            with spacer.expander("▸ description"):
                if not job["desc_is_full"]:
                    st.caption("Excerpt only — run enrich.py for the full posting.")
                st.write(job["description"] or "_No description._")


def render_pipeline(conn):
    m = pipeline_metrics(conn)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Applied today", f"{m['applied_today']} / {DAILY_GOAL}")
    c2.metric("Applied this week", m["applied_week"])
    c3.metric("Total tracked", m["tracked"])
    c4.metric("Response rate", f"{m['response_rate']:.0f}%")

    st.subheader("Last 14 days")
    daily = applications_per_day(conn)
    st.bar_chart(daily, height=240)
    hits = "".join(
        f'<span style="display:inline-block;width:26px;height:26px;line-height:26px;'
        f"text-align:center;margin-right:3px;border-radius:5px;font-size:12px;"
        f"background:{'#1a7f37' if v >= DAILY_GOAL else '#eaeef2'};"
        f'color:{"#fff" if v >= DAILY_GOAL else "#57606a"}">{v}</span>'
        for v in daily["applied"]
    )
    st.markdown(hits, unsafe_allow_html=True)
    st.caption(f"Goal is {DAILY_GOAL}/day — green squares are days you hit it.")

    st.subheader("All applications")
    rows = conn.execute(
        """SELECT a.job_id, j.title, j.company, a.status, a.applied_at, a.notes, j.url
           FROM applications a LEFT JOIN jobs j ON j.id = a.job_id
           ORDER BY a.applied_at DESC"""
    ).fetchall()
    if not rows:
        st.info("No applications tracked yet.")
        return

    df = pd.DataFrame([dict(r) for r in rows])
    df["applied_at"] = df["applied_at"].str.slice(0, 10)  # date is enough to scan
    edited = st.data_editor(
        df,
        column_config={
            "job_id": None,
            "status": st.column_config.SelectboxColumn("status", options=STATUSES, required=True),
            "notes": st.column_config.TextColumn("notes", width="large"),
            "url": st.column_config.LinkColumn("link", display_text="open"),
            "applied_at": st.column_config.TextColumn("applied", disabled=True),
            "title": st.column_config.TextColumn("title", disabled=True),
            "company": st.column_config.TextColumn("company", disabled=True),
        },
        hide_index=True,
        use_container_width=True,
        key="pipeline_editor",
    )
    changes = edited.compare(df)
    if not changes.empty:
        for idx in changes.index:
            row = edited.loc[idx]
            conn.execute(
                "UPDATE applications SET status = ?, notes = ? WHERE job_id = ?",
                (row["status"], row["notes"], row["job_id"]),
            )
        conn.commit()
        st.toast(f"Saved {len(changes)} change(s)")


def render_tuning(conn):
    st.caption(f"score = {scoring.FORMULA}   ·   click any column header to sort")
    df = tuning_rows(conn)
    if df.empty:
        st.info("Nothing scored yet.")
        return
    st.dataframe(df, hide_index=True, use_container_width=True, height=420)

    left, right = st.columns(2)
    with left:
        st.subheader("Most frequent matched keywords")
        st.dataframe(keyword_frequency(conn), hide_index=True, use_container_width=True)
    with right:
        st.subheader("Most frequent terms in dropped jobs")
        table, dropped = dropped_terms(conn)
        st.caption(f"{dropped} jobs dropped by hard filters or no title match")
        st.dataframe(table, hide_index=True, use_container_width=True)


if __name__ == "__main__":
    main()
