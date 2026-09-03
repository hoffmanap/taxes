#!/usr/bin/env python3
"""
jobs_density_pull.py

Pulls total workplace employment (jobs) per city from the Census Bureau's
LEHD LODES (Longitudinal Employer-Household Dynamics, Origin-Destination
Employment Statistics) dataset, for the same 150 cities used throughout this
project, and computes jobs density (jobs / land area) -- the commercial-
activity analogue to the population-density measure used everywhere else in
the dashboard.

WHY THIS EXISTS
----------------
Testing whether "business density" matters for municipal spending and the
tax rate the same way population density does, and whether cities with a
more commercial job base show any relief in the homeowner-side effective
tax rate (the "shift the burden to businesses" hypothesis).

NO API KEY NEEDED. Unlike the ACS pull, LODES is published as bulk flat
files with no registration required. This script downloads ~39 states'
worth of data (~500MB total) directly from lehd.ces.census.gov.

WHAT THIS DOES NOT ANSWER
----------------------------
Jobs density is a proxy for commercial *activity*, not commercial *property
tax base*. There is no unified national dataset of assessed value split by
residential vs. commercial/industrial classification -- that's a state and
county function, and every state's assessor system works differently (see
the accompanying analysis notes for why this couldn't be pulled the same
way as everything else). A city could have a lot of jobs without much of
that translating into local commercial property tax base (e.g. leased
office space owned by an out-of-state REIT). Treat jobs density as
suggestive of the commercial question, not a direct measurement of it.

OUTPUT
------
Writes `jobs_density_150.csv`: one row per city with total jobs (C000),
jobs density (jobs per square mile), jobs per resident, and a rough
"commercial-coded" job subtotal (retail, real estate, professional
services, admin/waste, arts/entertainment, accommodation/food -- the WAC
NAICS sectors most associated with commercial rather than industrial or
institutional space).

DATA VINTAGE NOTE
-------------------
Uses 2021 LODES data (matches reasonably closely with the 2022 ACS/Census
of Governments vintage used elsewhere in this project). Alaska is excluded:
its LODES data hasn't been updated since 2016, too stale to mix with the
other 149 cities' 2021 figures. Anchorage will show up with null jobs data
in the output for that reason.
"""

import gzip
import io
import sys
import time
import urllib.error
import urllib.request

import pandas as pd

LODES_YEAR = 2021
LODES_BASE = "https://lehd.ces.census.gov/data/lodes/LODES8"

# Census Bureau state FIPS -> USPS abbreviation, for the states actually
# needed by BASE_DATA below. Alaska (02) is intentionally excluded (see
# docstring). Add more states here if BASE_DATA is extended to include
# cities outside this list.
FIPS_TO_USPS = {
    '01': 'al', '04': 'az', '05': 'ar', '06': 'ca', '08': 'co', '11': 'dc',
    '12': 'fl', '13': 'ga', '15': 'hi', '16': 'id', '17': 'il', '18': 'in',
    '19': 'ia', '20': 'ks', '21': 'ky', '22': 'la', '24': 'md', '25': 'ma',
    '26': 'mi', '27': 'mn', '29': 'mo', '31': 'ne', '32': 'nv', '34': 'nj',
    '35': 'nm', '36': 'ny', '37': 'nc', '39': 'oh', '40': 'ok', '41': 'or',
    '42': 'pa', '44': 'ri', '46': 'sd', '47': 'tn', '48': 'tx', '49': 'ut',
    '51': 'va', '53': 'wa', '55': 'wi',
}

# WAC NAICS-sector columns most associated with commercial (as opposed to
# industrial/institutional) activity: Retail (CNS07), Real Estate (CNS10),
# Professional/Scientific/Technical (CNS11), Admin/Waste Mgmt (CNS13),
# Arts/Entertainment/Recreation (CNS16), Accommodation/Food Service (CNS17).
COMMERCIAL_SECTOR_COLS = ['CNS07', 'CNS10', 'CNS11', 'CNS13', 'CNS16', 'CNS17']


def fetch_gz_csv(url, retries=3, backoff=2.0):
    """Download a .csv.gz URL and return it as a pandas-ready decompressed bytes buffer."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "python-urllib"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                compressed = resp.read()
            return gzip.decompress(compressed)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last_err = str(e)
        except Exception as e:
            last_err = str(e)
        time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}\n  -> {last_err}")


def pull_state(usps, state_fips):
    """Download WAC (jobs by block) and the block->place crosswalk for one state,
    join them, and return jobs totals aggregated to the place (city) level."""
    wac_url = f"{LODES_BASE}/{usps}/wac/{usps}_wac_S000_JT00_{LODES_YEAR}.csv.gz"
    xwalk_url = f"{LODES_BASE}/{usps}/{usps}_xwalk.csv.gz"

    wac_bytes = fetch_gz_csv(wac_url)
    wac = pd.read_csv(io.BytesIO(wac_bytes), dtype={"w_geocode": str})

    xwalk_bytes = fetch_gz_csv(xwalk_url)
    xwalk = pd.read_csv(
        io.BytesIO(xwalk_bytes), dtype={"tabblk2020": str, "stplc": str},
        usecols=["tabblk2020", "stplc"]
    )

    merged = wac.merge(xwalk, left_on="w_geocode", right_on="tabblk2020", how="left")
    agg_cols = ["C000"] + [c for c in COMMERCIAL_SECTOR_COLS if c in merged.columns]
    grp = merged.groupby("stplc")[agg_cols].sum().reset_index()
    grp["state_fips"] = state_fips
    return grp


def main():
    # BASE_DATA supplies stplc (state+place FIPS) and land_area_sqmi for the
    # match/merge step. Pull these from the existing merged ACS dataset if
    # present in the working directory; otherwise the script still runs the
    # state-level pulls and writes the raw place-level jobs table.
    try:
        base = pd.read_csv(
            "acs_income_housing_merged_150.csv",
            dtype={"state_fips": str, "place_fips": str}
        )
        base["state_fips"] = base["state_fips"].str.zfill(2)
        base["place_fips"] = base["place_fips"].str.zfill(5)
        base["stplc"] = base["state_fips"] + base["place_fips"]
        have_base = True
        states_needed = sorted(base["state_fips"].unique())
    except FileNotFoundError:
        print("acs_income_housing_merged_150.csv not found in this directory -- "
              "will still pull jobs data for all mapped states, but can't merge "
              "or compute jobs density without land area / place codes.",
              file=sys.stderr)
        have_base = False
        states_needed = list(FIPS_TO_USPS.keys())

    results = []
    n = len(states_needed)
    for i, state_fips in enumerate(states_needed, start=1):
        usps = FIPS_TO_USPS.get(state_fips)
        if usps is None:
            print(f"[{i}/{n}] state_fips {state_fips}: no USPS mapping (Alaska, or "
                  f"a state not in FIPS_TO_USPS) -- skipping", file=sys.stderr)
            continue
        print(f"[{i}/{n}] {usps} ...", end=" ", flush=True)
        try:
            grp = pull_state(usps, state_fips)
            results.append(grp)
            print(f"ok ({len(grp)} places)")
        except Exception as e:
            print(f"FAILED ({e})")

    jobs = pd.concat(results, ignore_index=True)
    jobs["stplc"] = jobs["stplc"].str.zfill(7)

    if not have_base:
        jobs.to_csv("jobs_by_place_raw.csv", index=False)
        print(f"\nWrote jobs_by_place_raw.csv ({len(jobs)} place rows, no city merge).")
        return

    merged = base.merge(
        jobs[["stplc", "C000"] + [c for c in COMMERCIAL_SECTOR_COLS if c in jobs.columns]],
        on="stplc", how="left"
    )
    merged["jobs_density"] = merged["C000"] / merged["land_area_sqmi"]
    merged["jobs_per_resident"] = merged["C000"] / merged["pop"]
    commercial_cols_present = [c for c in COMMERCIAL_SECTOR_COLS if c in merged.columns]
    if commercial_cols_present:
        merged["commercial_jobs"] = merged[commercial_cols_present].sum(axis=1)
        merged["pct_commercial_jobs"] = 100 * merged["commercial_jobs"] / merged["C000"]

    out_cols = ["gid", "name", "pop", "land_area_sqmi", "C000", "jobs_density",
                "jobs_per_resident"] + commercial_cols_present
    if "commercial_jobs" in merged.columns:
        out_cols += ["commercial_jobs", "pct_commercial_jobs"]
    out_cols = [c for c in out_cols if c in merged.columns]

    merged[out_cols].rename(columns={"C000": "total_jobs"}).to_csv(
        "jobs_density_150.csv", index=False
    )

    matched = merged["C000"].notna().sum()
    print(f"\nWrote jobs_density_150.csv: {matched} of {len(merged)} cities matched.")
    missing = merged[merged["C000"].isna()]["name"].tolist()
    if missing:
        print("  Cities with no jobs data (Alaska excluded by design; consolidated "
              "governments missing ACS data are also expected here):")
        for name in missing:
            print(f"    - {name}")


if __name__ == "__main__":
    main()
