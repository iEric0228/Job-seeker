"""Tests for the scorer. This is the only test file in the project."""

from datetime import UTC, datetime, timedelta

import pytest

from score import GAP_PENALTY_CAP, KEYWORD_CAP, compile_profile, score_job

RESUME = {
    "match": {
        "target_titles": [
            {"pattern": "cloud support engineer", "weight": 10},
            {"pattern": "devops engineer", "weight": 8},
            {"pattern": "systems? engineer", "weight": 6},
        ],
        "exclude_titles": [
            r"\b(director|vp)\b",
            r"\b(senior|sr\.?|staff|lead)\b",
            r"\bmanager\b",
        ],
        "exclude_description": [r"\bpolygraph\b", "top secret clearance"],
        "keywords": {
            "strong": {"aws": 5, "linux": 4, "troubleshooting": 4, "terraform": 3},
            "nice": {"dns": 1, "ansible": 1, "ci/cd": 1},
            "gaps": ["java", "c++", "salesforce", "sap", "vmware"],
        },
        "locations": [
            {"pattern": r"Remote \(US\)", "weight": 10},
            {"pattern": "boston|cambridge", "weight": 9},
            {"pattern": r"massachusetts|\bma\b", "weight": 7},
        ],
        "salary_floor": 75000,
        "max_years_experience": 2,
        "levels": ["internship", "entry", "mid"],
        "countries": ["US"],
    }
}


@pytest.fixture
def profile():
    return compile_profile(RESUME)


def make_job(**overrides):
    job = {
        "title": "Cloud Support Engineer",
        "description": "Work with aws and linux.",
        "location": "Boston, MA",
        "remote": 0,
        "salary_max": 120000,
        "posted_at": None,
        "min_years_exp": None,
        "level": "entry",
        "country": "US",
    }
    job.update(overrides)
    return job


def hours_ago(n):
    return (datetime.now(UTC) - timedelta(hours=n)).isoformat()


# --- hard filters ---------------------------------------------------------- #


def test_exclude_title_drops_the_job(profile):
    result = score_job(make_job(title="Director of Cloud Support Engineer"), profile)
    assert result["dropped"].startswith("exclude_title:")


def test_exclude_description_drops_the_job(profile):
    job = make_job(description="Requires an active top secret clearance and aws.")
    assert score_job(job, profile)["dropped"].startswith("exclude_description:")


def test_no_title_match_drops_the_job(profile):
    assert score_job(make_job(title="Pastry Chef"), profile)["dropped"] == "no_title_match"


def test_off_target_titles_report_no_title_match_not_an_exclusion(profile):
    """Filter order is a diagnostic choice, so it has to stay this way.

    A "Senior Marketing Manager" is not a job you lost to the seniority rule --
    it was never a candidate. Blaming the exclusion makes that rule look
    expensive in `score.py --why` and hides the real shape of the funnel.
    """
    assert score_job(make_job(title="Senior Marketing Manager"), profile)["dropped"] == (
        "no_title_match"
    )
    # ...but a target role that happens to be senior IS a real loss, and says so.
    assert score_job(make_job(title="Senior Cloud Support Engineer"), profile)[
        "dropped"
    ].startswith("exclude_title:")


def test_reordering_filters_changes_attribution_not_outcome(profile):
    """Every hard filter is ANDed, so the surviving set must be order-independent."""
    survivors = [
        make_job(title="Cloud Support Engineer"),  # passes everything
        make_job(title="DevOps Engineer", min_years_exp=1),
    ]
    rejects = [
        make_job(title="Senior Marketing Manager"),
        make_job(title="Senior Cloud Support Engineer"),
        make_job(title="Cloud Support Engineer", min_years_exp=9),
        make_job(title="Cloud Support Engineer", country="GB"),
        make_job(title="Cloud Support Engineer", description="requires a polygraph"),
        make_job(title="Pastry Chef"),
    ]
    for job in survivors:
        assert "dropped" not in score_job(job, profile), job["title"]
    for job in rejects:
        assert "dropped" in score_job(job, profile), job["title"]


def test_title_score_takes_the_highest_matching_weight(profile):
    # matches both "devops engineer" (8) and "systems engineer" (6)
    job = make_job(title="DevOps Engineer / Systems Engineer", description="")
    assert score_job(job, profile)["title_score"] == 8


# --- experience and geography filters --------------------------------------- #


def test_job_demanding_more_years_than_wanted_is_dropped(profile):
    result = score_job(make_job(min_years_exp=5), profile)
    assert result["dropped"] == "needs_5y_experience"


def test_job_at_the_year_limit_is_kept(profile):
    assert "dropped" not in score_job(make_job(min_years_exp=2), profile)


def test_job_stating_no_year_requirement_is_kept(profile):
    """Most postings never name a number; the filter must not silently eat them."""
    assert "dropped" not in score_job(make_job(min_years_exp=None), profile)


def test_senior_level_is_dropped_when_not_in_allowed_levels(profile):
    assert score_job(make_job(level="senior"), profile)["dropped"] == "level:senior"


def test_internship_and_entry_are_kept(profile):
    for level in ("internship", "entry", "mid"):
        assert "dropped" not in score_job(make_job(level=level), profile), level


def test_foreign_job_is_dropped(profile):
    """Greenhouse and Lever boards return roles worldwide."""
    assert score_job(make_job(country="GB"), profile)["dropped"] == "country:GB"
    assert "dropped" not in score_job(make_job(country="US"), profile)


def test_unknown_level_or_country_is_kept(profile):
    assert "dropped" not in score_job(make_job(level=None, country=None), profile)


def test_filters_are_inactive_when_unconfigured():
    """A profile with no experience or country rules must not drop anything."""
    bare = compile_profile(
        {"match": {**RESUME["match"], "max_years_experience": None, "levels": [], "countries": []}}
    )
    job = make_job(min_years_exp=20, level="senior", country="GB")
    assert "dropped" not in score_job(job, bare)


# --- components ------------------------------------------------------------ #


def test_keyword_score_counts_each_term_once(profile):
    job = make_job(description="aws aws aws aws linux linux")
    result = score_job(job, profile)
    assert result["keyword_score"] == 9  # aws 5 + linux 4, not 5*4 + 4*2
    assert sorted(result["matched_keywords"]) == ["aws", "linux"]


def test_keyword_score_is_capped_at_30(profile):
    rich = compile_profile(
        {
            "match": {
                **RESUME["match"],
                "keywords": {
                    "strong": {f"term{i}": 9 for i in range(10)},
                    "nice": {},
                    "gaps": [],
                },
            }
        }
    )
    job = make_job(description=" ".join(f"term{i}" for i in range(10)))
    result = score_job(job, rich)
    assert result["keyword_score"] == KEYWORD_CAP  # 90 raw, capped
    assert len(result["matched_keywords"]) == 10  # chips still show everything


def test_keyword_matching_survives_punctuation(profile):
    result = score_job(make_job(description="strong ci/cd background"), profile)
    assert "ci/cd" in result["matched_keywords"]


def test_keyword_matching_respects_word_boundaries(profile):
    # "flawson" must not match "aws"
    result = score_job(make_job(description="we use flawson and dnsomething"), profile)
    assert result["matched_keywords"] == []


def test_remote_flag_satisfies_the_remote_us_location(profile):
    job = make_job(location="Anywhere", remote=1)
    assert score_job(job, profile)["location_score"] == 10


def test_location_score_takes_the_highest_matching_weight(profile):
    assert score_job(make_job(location="Boston, MA"), profile)["location_score"] == 9
    assert score_job(make_job(location="Worcester, MA"), profile)["location_score"] == 7
    assert score_job(make_job(location="Dallas, TX"), profile)["location_score"] == 0


@pytest.mark.parametrize(
    "age_hours,expected",
    [(1, 3), (47, 3), (49, 1), (24 * 6, 1), (24 * 8, 0)],
)
def test_freshness_bands(profile, age_hours, expected):
    job = make_job(posted_at=hours_ago(age_hours))
    assert score_job(job, profile)["freshness_score"] == expected


def test_missing_posted_at_scores_no_freshness(profile):
    assert score_job(make_job(posted_at=None), profile)["freshness_score"] == 0


# --- penalties ------------------------------------------------------------- #


def test_salary_floor_penalty(profile):
    assert score_job(make_job(salary_max=60000), profile)["penalty"] == 5
    assert score_job(make_job(salary_max=75000), profile)["penalty"] == 0


def test_missing_salary_is_not_penalised(profile):
    assert score_job(make_job(salary_max=None), profile)["penalty"] == 0


def test_gap_penalty_is_capped_at_6(profile):
    job = make_job(description="java c++ salesforce sap vmware everywhere")
    result = score_job(job, profile)
    assert result["penalty"] == GAP_PENALTY_CAP  # 5 gaps x2 = 10, capped at 6
    assert len(result["gap_flags"]) == 5  # every gap still surfaces as a chip


def test_gap_penalty_accumulates_below_the_cap(profile):
    assert score_job(make_job(description="java only"), profile)["penalty"] == 2
    assert score_job(make_job(description="java and c++"), profile)["penalty"] == 4


def test_penalties_stack(profile):
    job = make_job(description="java and c++ and salesforce", salary_max=50000)
    assert score_job(job, profile)["penalty"] == GAP_PENALTY_CAP + 5


# --- end to end ------------------------------------------------------------ #


def test_full_score_of_a_known_fixture(profile):
    job = {
        "title": "Cloud Support Engineer",
        "description": (
            "Troubleshooting aws workloads on linux. Terraform and ci/cd experience "
            "valued. Some java services in the estate."
        ),
        "location": "Boston, MA",
        "remote": 0,
        "salary_max": 110000,
        "posted_at": hours_ago(12),
    }
    result = score_job(job, profile)

    # title 10 -> x3 = 30
    # keywords: aws 5 + linux 4 + troubleshooting 4 + terraform 3 + ci/cd 1 = 17
    # location Boston = 9 ; freshness <48h = 3 ; penalty: one gap (java) = 2
    assert result["title_score"] == 10
    assert result["keyword_score"] == 17
    assert result["location_score"] == 9
    assert result["freshness_score"] == 3
    assert result["penalty"] == 2
    assert result["gap_flags"] == ["java"]
    assert result["score"] == 30 + 17 + 9 + 3 - 2 == 57


def test_title_dominates_at_equal_keyword_density(profile):
    """Title x3 is what separates two otherwise identical postings."""
    body = "aws linux terraform"
    top = make_job(title="Cloud Support Engineer", description=body)
    lower = make_job(title="Systems Engineer", description=body)
    assert score_job(top, profile)["score"] - score_job(lower, profile)["score"] == (10 - 6) * 3


def test_keyword_density_can_outrun_a_title_gap(profile):
    """Documents a real limit of the §7 formula, so it is not discovered by surprise.

    title contributes 0-30 (weight x3) and keywords contribute 0-30 (the cap),
    so a lower-tier title with a dense JD overtakes a top-tier title with a thin
    one once its keyword lead exceeds 3x the title-weight gap. Lower KEYWORD_CAP
    in score.py if the daily ten start drifting off-title.
    """
    thin_top_title = make_job(
        title="Cloud Support Engineer",
        description="Support our cloud.",
        location="Boston, MA",
    )
    dense_lower_title = make_job(
        title="Systems Engineer",
        description="aws linux terraform troubleshooting dns ansible ci/cd",
        location="Boston, MA",
    )
    thin = score_job(thin_top_title, profile)
    dense = score_job(dense_lower_title, profile)

    title_gap = (thin["title_score"] - dense["title_score"]) * 3  # 12
    keyword_gap = dense["keyword_score"] - thin["keyword_score"]  # 19
    assert keyword_gap > title_gap
    assert dense["score"] > thin["score"]
