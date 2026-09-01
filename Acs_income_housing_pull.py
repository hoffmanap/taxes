#!/usr/bin/env python3
"""
acs_income_housing_pull.py

Fetches median household income, median home value, median real-estate taxes
paid, and household/housing counts for the 50 largest U.S. cities from the
Census Bureau's American Community Survey (ACS) 5-Year API, merges them with
the density and municipal-finance figures already compiled from the 2022
Census of Governments, and computes the "normalized by area" measures needed
to look at density + home values + income together:

    - home value per square mile          (housing-value concentration)
    - income per square mile              (aggregate household-income concentration)
    - effective property tax rate         (median taxes paid / median home value)
    - property tax burden as % of income  (median taxes paid / median household income)
    - municipal spending per square mile  (aggregate direct spending concentration)
    - share of housing stock that is single-family (detached, and detached+attached)

WHY THIS EXISTS
----------------
The Census Data API now requires a free registration key for every query
(this changed since the underlying finance/land-area data for this project
was first pulled from bulk downloads that don't need a key). Get one here,
it's instant and free:

    https://api.census.gov/data/key_signup.html

Then, EITHER put your key directly in this file (see CENSUS_API_KEY below,
just after the imports) and run:
           python acs_income_housing_pull.py

    OR set it as an environment variable before running:
           export CENSUS_API_KEY="your_key_here"       (Mac/Linux)
           set CENSUS_API_KEY=your_key_here             (Windows cmd)
       and just run:  python acs_income_housing_pull.py

    OR pass it on the command line:
           python acs_income_housing_pull.py --key your_key_here

    Precedence if more than one is set: --key flag > environment variable >
    the hardcoded value in this file.

OUTPUT
------
Writes `acs_income_housing_merged.csv` in the current directory: one row per
city, joining the ACS pull with everything already computed (density, direct
general expenditure per capita, property tax revenue per capita, core
infrastructure spending per capita, consolidated-government flag, region).

WHAT'S EMBEDDED VS. WHAT'S FETCHED
-----------------------------------
The density/land-area/expenditure/tax-revenue figures for all 50 cities are
embedded below (BASE_DATA) exactly as compiled in the earlier analysis from
the 2022 Census of Governments bulk files -- no key needed for those, they
were already pulled. This script only needs to fetch the ACS variables that
weren't previously available: median household income, median home value,
median real estate taxes paid, and household/housing-unit counts.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

# ---------------------------------------------------------------------------
# PUT YOUR CENSUS API KEY HERE if you'd rather not use an environment
# variable or the --key flag. Get a free key at:
#     https://api.census.gov/data/key_signup.html
# Leave it as an empty string ("") to use the environment variable or --key
# flag instead.
# ---------------------------------------------------------------------------
CENSUS_API_KEY = ""

ACS_YEAR = 2022  # matches the 2022 Census of Governments finance data used elsewhere in this project
ACS_DATASET = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"

# ACS Detailed Table variables:
#   B19013_001E  Median household income in the past 12 months (dollars)
#   B25077_001E  Median value of owner-occupied housing units (dollars)
#   B11001_001E  Total households
#   B25003_001E  Total occupied housing units (owner + renter)
#   B25003_002E  Owner-occupied housing units
#   B25024_001E  Total housing units by units-in-structure (universe for this table --
#                slightly different universe than B25003, includes vacant units)
#   B25024_002E  1-unit, detached (the "classic" single-family home)
#   B25024_003E  1-unit, attached (townhomes/rowhouses -- still single-family in form,
#                just sharing a wall; usually counted as "single-family" in housing analyses)
# Median real estate taxes paid (B25103) is a "mortgage status by median taxes
# paid" table, so the variable ID for the "Total" line isn't a fixed constant
# across all years -- this script looks it up dynamically from the Census
# Bureau's own group metadata (see resolve_tax_variable()) so it doesn't
# silently break if the ID ever shifts.
BASE_VARS = {
    "median_household_income": "B19013_001E",
    "median_home_value": "B25077_001E",
    "total_households": "B11001_001E",
    "total_occupied_housing_units": "B25003_001E",
    "owner_occupied_housing_units": "B25003_002E",
    "total_units_by_structure": "B25024_001E",
    "units_1detached": "B25024_002E",
    "units_1attached": "B25024_003E",
}


def http_get_json(url, retries=3, backoff=2.0):
    """GET a URL and parse JSON, with basic retry on transient failures."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "python-urllib"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            last_err = f"HTTP {e.code}: {body[:300]}"
        except Exception as e:
            last_err = str(e)
        time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}\n  -> {last_err}")


def resolve_tax_variable(api_key):
    """
    Find the ACS variable ID for the "Total" median real-estate-taxes-paid
    line in table B25103 (Mortgage Status by Median Real Estate Taxes Paid).
    Looked up dynamically from Census metadata instead of hardcoded, since
    the exact ID has moved between vintages in the past.
    """
    url = f"{ACS_DATASET}/groups/B25103.json?key={api_key}" if api_key else f"{ACS_DATASET}/groups/B25103.json"
    try:
        meta = http_get_json(url)
    except Exception as e:
        print(f"  [warn] could not resolve B25103 metadata ({e}); "
              f"falling back to B25103_001E", file=sys.stderr)
        return "B25103_001E"

    variables = meta.get("variables", {})
    # Prefer the variable whose label is exactly "Total" at the first level
    # (not broken out by "With a mortgage" / "Not mortgaged")
    candidates = []
    for var_id, info in variables.items():
        if not var_id.endswith("E"):
            continue
        label = info.get("label", "")
        # Typical label shape: "Estimate!!Median real estate taxes paid (dollars)!!Total:"
        if "Total" in label and "real estate taxes" in label.lower():
            candidates.append((var_id, label))
    if candidates:
        # shortest label = least broken-out = the overall total line
        candidates.sort(key=lambda x: len(x[1]))
        return candidates[0][0]

    print("  [warn] could not find a clear 'Total' variable in B25103; "
          "falling back to B25103_001E", file=sys.stderr)
    return "B25103_001E"


def fetch_acs_for_place(state_fips, place_fips, variables, api_key):
    """Fetch a set of ACS variables for one Census place. Returns dict var->value (float or None)."""
    var_list = ",".join(["NAME"] + list(variables.values()))
    qs = {
        "get": var_list,
        "for": f"place:{place_fips}",
        "in": f"state:{state_fips}",
    }
    if api_key:
        qs["key"] = api_key
    url = f"{ACS_DATASET}?{urllib.parse.urlencode(qs)}"
    data = http_get_json(url)
    header, row = data[0], data[1]
    result = dict(zip(header, row))
    out = {}
    for friendly_name, var_id in variables.items():
        raw = result.get(var_id)
        try:
            val = float(raw)
            # Census uses large negative sentinel codes for "not available"
            if val < 0:
                val = None
        except (TypeError, ValueError):
            val = None
        out[friendly_name] = val
    return out


BASE_DATA = json.loads(r'''
[
    {
        "gid": "362061194805",
        "name": "NEW YORK CITY",
        "state_fips": "36",
        "place_fips": "51000",
        "pop": 8253213,
        "land_area_sqmi": 300.457,
        "density": 27468.87,
        "direct_gen_exp_pc": 13744.92,
        "property_tax_pc": 3587.55,
        "core_infra_exp_pc": 1582.03,
        "consolidated_govt": 1,
        "region": "Northeast"
    },
    {
        "gid": "062075161258",
        "name": "SAN FRANCISCO CITY AND COUNTY",
        "state_fips": "06",
        "place_fips": "67000",
        "pop": 866606,
        "land_area_sqmi": 46.7,
        "density": 18556.87,
        "direct_gen_exp_pc": 16144.45,
        "property_tax_pc": 3430.59,
        "core_infra_exp_pc": 2508.24,
        "consolidated_govt": 1,
        "region": "West"
    },
    {
        "gid": "252025128108",
        "name": "BOSTON CITY",
        "state_fips": "25",
        "place_fips": "07000",
        "pop": 691531,
        "land_area_sqmi": 48.34,
        "density": 14305.56,
        "direct_gen_exp_pc": 7428.96,
        "property_tax_pc": 4146.55,
        "core_infra_exp_pc": 1609.93,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "122086194757",
        "name": "MIAMI CITY",
        "state_fips": "12",
        "place_fips": "45000",
        "pop": 471525,
        "land_area_sqmi": 35.996,
        "density": 13099.37,
        "direct_gen_exp_pc": 2741.53,
        "property_tax_pc": 1113.19,
        "core_infra_exp_pc": 1321.83,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "172031162236",
        "name": "CHICAGO CITY",
        "state_fips": "17",
        "place_fips": "14000",
        "pop": 2677643,
        "land_area_sqmi": 227.748,
        "density": 11757.04,
        "direct_gen_exp_pc": 3985.2,
        "property_tax_pc": 1250.36,
        "core_infra_exp_pc": 1250.35,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "422101133602",
        "name": "PHILADELPHIA CITY",
        "state_fips": "42",
        "place_fips": "60000",
        "pop": 1578487,
        "land_area_sqmi": 134.356,
        "density": 11748.54,
        "direct_gen_exp_pc": 5747.03,
        "property_tax_pc": 458.24,
        "core_infra_exp_pc": 1378.46,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "112001124214",
        "name": "WASHINGTON DC CITY",
        "state_fips": "11",
        "place_fips": "50000",
        "pop": 712816,
        "land_area_sqmi": 61.126,
        "density": 11661.42,
        "direct_gen_exp_pc": 26074.73,
        "property_tax_pc": 4160.09,
        "core_infra_exp_pc": 2944.11,
        "consolidated_govt": 1,
        "region": "South"
    },
    {
        "gid": "532033184255",
        "name": "SEATTLE CITY",
        "state_fips": "53",
        "place_fips": "63000",
        "pop": 769714,
        "land_area_sqmi": 84.0,
        "density": 9163.26,
        "direct_gen_exp_pc": 3790.64,
        "property_tax_pc": 989.54,
        "core_infra_exp_pc": 1719.88,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062037161173",
        "name": "LONG BEACH CITY",
        "state_fips": "06",
        "place_fips": "43000",
        "pop": 454681,
        "land_area_sqmi": 50.672,
        "density": 8973.02,
        "direct_gen_exp_pc": 4728.47,
        "property_tax_pc": 499.39,
        "core_infra_exp_pc": 1452.24,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062037161174",
        "name": "LOS ANGELES CITY",
        "state_fips": "06",
        "place_fips": "44000",
        "pop": 3970219,
        "land_area_sqmi": 470.517,
        "density": 8437.99,
        "direct_gen_exp_pc": 3332.06,
        "property_tax_pc": 659.32,
        "core_infra_exp_pc": 1572.76,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "272053193551",
        "name": "MINNEAPOLIS CITY",
        "state_fips": "27",
        "place_fips": "43000",
        "pop": 433111,
        "land_area_sqmi": 54.0,
        "density": 8020.57,
        "direct_gen_exp_pc": 2779.08,
        "property_tax_pc": 822.19,
        "core_infra_exp_pc": 1521.68,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "062001123093",
        "name": "OAKLAND CITY",
        "state_fips": "06",
        "place_fips": "53000",
        "pop": 424891,
        "land_area_sqmi": 55.963,
        "density": 7592.36,
        "direct_gen_exp_pc": 3963.56,
        "property_tax_pc": 1101.73,
        "core_infra_exp_pc": 1708.09,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "242510208246",
        "name": "BALTIMORE CITY",
        "state_fips": "24",
        "place_fips": "04000",
        "pop": 586131,
        "land_area_sqmi": 80.946,
        "density": 7241.01,
        "direct_gen_exp_pc": 7929.53,
        "property_tax_pc": 1810.5,
        "core_infra_exp_pc": 2292.79,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "552079136562",
        "name": "MILWAUKEE CITY",
        "state_fips": "55",
        "place_fips": "53000",
        "pop": 589067,
        "land_area_sqmi": 96.173,
        "density": 6125.08,
        "direct_gen_exp_pc": 2068.59,
        "property_tax_pc": 614.83,
        "core_infra_exp_pc": 1229.16,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "062085100745",
        "name": "SAN JOSE CITY",
        "state_fips": "06",
        "place_fips": "68000",
        "pop": 1013616,
        "land_area_sqmi": 177.939,
        "density": 5696.42,
        "direct_gen_exp_pc": 2311.35,
        "property_tax_pc": 705.02,
        "core_infra_exp_pc": 1325.6,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062067161241",
        "name": "SACRAMENTO CITY",
        "state_fips": "06",
        "place_fips": "64000",
        "pop": 512838,
        "land_area_sqmi": 98.646,
        "density": 5198.77,
        "direct_gen_exp_pc": 2992.53,
        "property_tax_pc": 413.42,
        "core_infra_exp_pc": 1257.55,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "412051211254",
        "name": "PORTLAND CITY",
        "state_fips": "41",
        "place_fips": "59000",
        "pop": 656751,
        "land_area_sqmi": 133.488,
        "density": 4919.93,
        "direct_gen_exp_pc": 2642.28,
        "property_tax_pc": 1105.99,
        "core_infra_exp_pc": 1385.68,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "082031194647",
        "name": "DENVER CITY AND COUNTY",
        "state_fips": "08",
        "place_fips": "20000",
        "pop": 735538,
        "land_area_sqmi": 153.074,
        "density": 4805.11,
        "direct_gen_exp_pc": 5908.52,
        "property_tax_pc": 733.98,
        "core_infra_exp_pc": 1261.61,
        "consolidated_govt": 1,
        "region": "West"
    },
    {
        "gid": "262163166817",
        "name": "DETROIT CITY",
        "state_fips": "26",
        "place_fips": "22000",
        "pop": 665369,
        "land_area_sqmi": 138.735,
        "density": 4795.97,
        "direct_gen_exp_pc": 3151.01,
        "property_tax_pc": 508.43,
        "core_infra_exp_pc": 1438.1,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "322003169988",
        "name": "LAS VEGAS CITY",
        "state_fips": "32",
        "place_fips": "40000",
        "pop": 662368,
        "land_area_sqmi": 141.862,
        "density": 4669.1,
        "direct_gen_exp_pc": 1300.52,
        "property_tax_pc": 246.65,
        "core_infra_exp_pc": 546.33,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062019189636",
        "name": "FRESNO CITY",
        "state_fips": "06",
        "place_fips": "27000",
        "pop": 530267,
        "land_area_sqmi": 115.818,
        "density": 4578.45,
        "direct_gen_exp_pc": 1613.41,
        "property_tax_pc": 291.95,
        "core_infra_exp_pc": 935.66,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062073207598",
        "name": "SAN DIEGO CITY",
        "state_fips": "06",
        "place_fips": "66000",
        "pop": 1422420,
        "land_area_sqmi": 326.087,
        "density": 4362.09,
        "direct_gen_exp_pc": 3061.26,
        "property_tax_pc": 477.64,
        "core_infra_exp_pc": 1397.44,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "482439194811",
        "name": "ARLINGTON CITY",
        "state_fips": "48",
        "place_fips": "04000",
        "pop": 398864,
        "land_area_sqmi": 95.842,
        "density": 4161.68,
        "direct_gen_exp_pc": 1245.32,
        "property_tax_pc": 429.31,
        "core_infra_exp_pc": 724.5,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "392049209050",
        "name": "COLUMBUS CITY",
        "state_fips": "39",
        "place_fips": "18000",
        "pop": 903852,
        "land_area_sqmi": 220.725,
        "density": 4094.92,
        "direct_gen_exp_pc": 2163.62,
        "property_tax_pc": 67.44,
        "core_infra_exp_pc": 1197.19,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "482113187649",
        "name": "DALLAS CITY",
        "state_fips": "48",
        "place_fips": "19000",
        "pop": 1343266,
        "land_area_sqmi": 339.676,
        "density": 3954.55,
        "direct_gen_exp_pc": 2887.05,
        "property_tax_pc": 932.8,
        "core_infra_exp_pc": 1205.83,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "132121194678",
        "name": "ATLANTA CITY",
        "state_fips": "13",
        "place_fips": "04000",
        "pop": 512550,
        "land_area_sqmi": 135.253,
        "density": 3789.56,
        "direct_gen_exp_pc": 2348.66,
        "property_tax_pc": 1033.7,
        "core_infra_exp_pc": 1024.77,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "042013100250",
        "name": "MESA CITY",
        "state_fips": "04",
        "place_fips": "46000",
        "pop": 528159,
        "land_area_sqmi": 141.384,
        "density": 3735.63,
        "direct_gen_exp_pc": 1499.02,
        "property_tax_pc": 98.32,
        "core_infra_exp_pc": 1077.07,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "482201176169",
        "name": "HOUSTON CITY",
        "state_fips": "48",
        "place_fips": "35000",
        "pop": 2316120,
        "land_area_sqmi": 640.606,
        "density": 3615.51,
        "direct_gen_exp_pc": 2045.12,
        "property_tax_pc": 693.06,
        "core_infra_exp_pc": 900.72,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "122057161633",
        "name": "TAMPA CITY",
        "state_fips": "12",
        "place_fips": "71000",
        "pop": 407599,
        "land_area_sqmi": 113.638,
        "density": 3586.82,
        "direct_gen_exp_pc": 3027.23,
        "property_tax_pc": 591.93,
        "core_infra_exp_pc": 1471.34,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "312055208698",
        "name": "OMAHA CITY",
        "state_fips": "31",
        "place_fips": "37000",
        "pop": 478393,
        "land_area_sqmi": 142.98,
        "density": 3345.87,
        "direct_gen_exp_pc": 1606.84,
        "property_tax_pc": 423.01,
        "core_infra_exp_pc": 978.68,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "042013207536",
        "name": "PHOENIX CITY",
        "state_fips": "04",
        "place_fips": "55000",
        "pop": 1708127,
        "land_area_sqmi": 518.331,
        "density": 3295.44,
        "direct_gen_exp_pc": 1597.18,
        "property_tax_pc": 169.4,
        "core_infra_exp_pc": 751.86,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "372183171305",
        "name": "RALEIGH CITY",
        "state_fips": "37",
        "place_fips": "55000",
        "pop": 474414,
        "land_area_sqmi": 149.447,
        "density": 3174.46,
        "direct_gen_exp_pc": 1745.28,
        "property_tax_pc": 614.88,
        "core_infra_exp_pc": 858.56,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "482029175988",
        "name": "SAN ANTONIO CITY",
        "state_fips": "48",
        "place_fips": "65000",
        "pop": 1567118,
        "land_area_sqmi": 498.922,
        "density": 3141.01,
        "direct_gen_exp_pc": 1755.4,
        "property_tax_pc": 411.45,
        "core_infra_exp_pc": 927.88,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "482453176394",
        "name": "AUSTIN CITY",
        "state_fips": "48",
        "place_fips": "05000",
        "pop": 995484,
        "land_area_sqmi": 326.365,
        "density": 3050.22,
        "direct_gen_exp_pc": 2800.45,
        "property_tax_pc": 941.6,
        "core_infra_exp_pc": 1255.49,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "352001170378",
        "name": "ALBUQUERQUE CITY",
        "state_fips": "35",
        "place_fips": "02000",
        "pop": 562540,
        "land_area_sqmi": 187.266,
        "density": 3003.96,
        "direct_gen_exp_pc": 1692.59,
        "property_tax_pc": 301.33,
        "core_infra_exp_pc": 947.7,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "212111165866",
        "name": "LOUISVILLE-JEFFERSON COUNTY METRO GOVERNMENT",
        "state_fips": "21",
        "place_fips": "48003",
        "pop": 767452,
        "land_area_sqmi": 263.065,
        "density": 2917.35,
        "direct_gen_exp_pc": 2642.25,
        "property_tax_pc": 269.57,
        "core_infra_exp_pc": 1007.84,
        "consolidated_govt": 1,
        "region": "South"
    },
    {
        "gid": "372119194726",
        "name": "CHARLOTTE CITY",
        "state_fips": "37",
        "place_fips": "12000",
        "pop": 900350,
        "land_area_sqmi": 310.759,
        "density": 2897.26,
        "direct_gen_exp_pc": 2296.67,
        "property_tax_pc": 592.6,
        "core_infra_exp_pc": 945.06,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "482439176375",
        "name": "FORT WORTH CITY",
        "state_fips": "48",
        "place_fips": "27000",
        "pop": 927720,
        "land_area_sqmi": 350.27,
        "density": 2648.59,
        "direct_gen_exp_pc": 1819.15,
        "property_tax_pc": 619.45,
        "core_infra_exp_pc": 1006.87,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "482141176118",
        "name": "EL PASO CITY",
        "state_fips": "48",
        "place_fips": "24000",
        "pop": 681534,
        "land_area_sqmi": 258.788,
        "density": 2633.56,
        "direct_gen_exp_pc": 1353.44,
        "property_tax_pc": 505.6,
        "core_infra_exp_pc": 698.12,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "182097207957",
        "name": "INDIANAPOLIS CITY",
        "state_fips": "18",
        "place_fips": "36003",
        "pop": 877903,
        "land_area_sqmi": 361.017,
        "density": 2431.75,
        "direct_gen_exp_pc": 4007.77,
        "property_tax_pc": 615.28,
        "core_infra_exp_pc": 838.57,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "082041161362",
        "name": "COLORADO SPRINGS CITY",
        "state_fips": "08",
        "place_fips": "16000",
        "pop": 482131,
        "land_area_sqmi": 201.868,
        "density": 2388.35,
        "direct_gen_exp_pc": 1444.75,
        "property_tax_pc": 101.92,
        "core_infra_exp_pc": 571.63,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "042019160840",
        "name": "TUCSON CITY",
        "state_fips": "04",
        "place_fips": "77000",
        "pop": 542629,
        "land_area_sqmi": 242.187,
        "density": 2240.54,
        "direct_gen_exp_pc": 1321.0,
        "property_tax_pc": 95.42,
        "core_infra_exp_pc": 887.6,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "472157175826",
        "name": "MEMPHIS CITY",
        "state_fips": "47",
        "place_fips": "48000",
        "pop": 649705,
        "land_area_sqmi": 296.202,
        "density": 2193.45,
        "direct_gen_exp_pc": 2161.31,
        "property_tax_pc": 645.18,
        "core_infra_exp_pc": 1274.69,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "402143212090",
        "name": "TULSA CITY",
        "state_fips": "40",
        "place_fips": "75000",
        "pop": 403166,
        "land_area_sqmi": 197.765,
        "density": 2038.61,
        "direct_gen_exp_pc": 2204.72,
        "property_tax_pc": 207.78,
        "core_infra_exp_pc": 958.47,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "512810202751",
        "name": "VIRGINIA BEACH CITY",
        "state_fips": "51",
        "place_fips": "82000",
        "pop": 451231,
        "land_area_sqmi": 244.719,
        "density": 1843.87,
        "direct_gen_exp_pc": 4657.45,
        "property_tax_pc": 1715.46,
        "core_infra_exp_pc": 1054.34,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "152003183701",
        "name": "HONOLULU CITY AND COUNTY",
        "state_fips": "15",
        "place_fips": "17000",
        "pop": 963826,
        "land_area_sqmi": 600.786,
        "density": 1604.28,
        "direct_gen_exp_pc": 2257.1,
        "property_tax_pc": 1408.87,
        "core_infra_exp_pc": 1000.63,
        "consolidated_govt": 1,
        "region": "West"
    },
    {
        "gid": "292095186236",
        "name": "KANSAS CITY CITY",
        "state_fips": "29",
        "place_fips": "38000",
        "pop": 497159,
        "land_area_sqmi": 314.68,
        "density": 1579.89,
        "direct_gen_exp_pc": 3514.73,
        "property_tax_pc": 376.0,
        "core_infra_exp_pc": 1419.84,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "472037175728",
        "name": "NASHVILLE-DAVIDSON COUNTY METROPOLITAN GOVERNMENT",
        "state_fips": "47",
        "place_fips": "52004",
        "pop": 694176,
        "land_area_sqmi": 475.566,
        "density": 1459.68,
        "direct_gen_exp_pc": 5157.86,
        "property_tax_pc": 2256.47,
        "core_infra_exp_pc": 933.26,
        "consolidated_govt": 1,
        "region": "South"
    },
    {
        "gid": "122031101942",
        "name": "JACKSONVILLE CITY",
        "state_fips": "12",
        "place_fips": "35000",
        "pop": 920570,
        "land_area_sqmi": 747.261,
        "density": 1231.93,
        "direct_gen_exp_pc": 3168.83,
        "property_tax_pc": 854.72,
        "core_infra_exp_pc": 1176.43,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "402109209170",
        "name": "OKLAHOMA CITY CITY",
        "state_fips": "40",
        "place_fips": "55000",
        "pop": 662314,
        "land_area_sqmi": 606.479,
        "density": 1092.06,
        "direct_gen_exp_pc": 1564.41,
        "property_tax_pc": 191.44,
        "core_infra_exp_pc": 942.22,
        "consolidated_govt": 0,
        "region": "South"
    }
]
''')


def main():
    parser = argparse.ArgumentParser(description="Pull ACS income/home-value data for the 50 largest U.S. cities.")
    parser.add_argument("--key", default=None,
                         help="Census API key. If omitted, falls back to the CENSUS_API_KEY "
                              "environment variable, then to the CENSUS_API_KEY value hardcoded "
                              "near the top of this file. Get a free key at "
                              "https://api.census.gov/data/key_signup.html")
    parser.add_argument("--out", default="acs_income_housing_merged.csv",
                         help="Output CSV path (default: acs_income_housing_merged.csv)")
    parser.add_argument("--sleep", type=float, default=0.3,
                         help="Seconds to pause between API calls (default 0.3, be polite to the API)")
    # parse_known_args (not parse_args) so this doesn't choke on extra
    # arguments Jupyter/IPython inject when running via %run or the kernel
    # (e.g. "-f ...kernel-xxxx.json") -- those are just ignored here.
    args, _unknown = parser.parse_known_args()

    # precedence: --key flag > environment variable > hardcoded CENSUS_API_KEY above
    api_key = args.key or os.environ.get("CENSUS_API_KEY") or CENSUS_API_KEY

    if not api_key:
        print("ERROR: no Census API key found.\n"
              "  Get a free key at https://api.census.gov/data/key_signup.html\n"
              "  then either: (1) paste it into CENSUS_API_KEY near the top of this file, "
              "(2) set it as the CENSUS_API_KEY environment variable, or (3) pass --key YOUR_KEY.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Resolving median-real-estate-taxes-paid variable for {ACS_YEAR} ACS 5-year...")
    tax_var = resolve_tax_variable(api_key)
    print(f"  using {tax_var}")

    variables = dict(BASE_VARS)
    variables["median_real_estate_taxes_paid"] = tax_var

    rows = []
    n = len(BASE_DATA)
    for i, city in enumerate(BASE_DATA, start=1):
        name = city["name"]
        print(f"[{i}/{n}] {name} ...", end=" ", flush=True)
        try:
            acs = fetch_acs_for_place(city["state_fips"], city["place_fips"], variables, api_key)
            print("ok")
        except Exception as e:
            print(f"FAILED ({e})")
            acs = {k: None for k in variables}
        merged = dict(city)
        merged.update(acs)
        rows.append(merged)
        time.sleep(args.sleep)

    # ---- compute the normalized / combined measures ----
    for r in rows:
        land_sqmi = r.get("land_area_sqmi")
        pop = r.get("pop")
        income = r.get("median_household_income")
        home_value = r.get("median_home_value")
        taxes_paid = r.get("median_real_estate_taxes_paid")
        households = r.get("total_households")
        exp_pc = r.get("direct_gen_exp_pc")

        # home value concentration: simple normalization Alex asked for directly
        r["home_value_per_sqmi"] = (home_value / land_sqmi) if (home_value and land_sqmi) else None

        # aggregate household income concentration (uses total households, not just median x population,
        # since "households" not "people" earn "household income")
        r["income_per_sqmi"] = (income * households / land_sqmi) if (income and households and land_sqmi) else None

        # the effective-tax-rate point: same tax bill on a cheaper house = higher effective rate
        r["effective_tax_rate_pct"] = (100 * taxes_paid / home_value) if (taxes_paid and home_value) else None

        # tax burden relative to income -- a second, income-based view of the same "who feels it more" question
        r["tax_burden_pct_of_income"] = (100 * taxes_paid / income) if (taxes_paid and income) else None

        # municipal spending concentration: total budget divided by land area, not by population --
        # answers "how much is being spent per square mile", the direct analogue to home_value_per_sqmi
        r["spending_per_sqmi"] = (exp_pc * pop / land_sqmi) if (exp_pc and pop and land_sqmi) else None

        # share of housing stock that is single-family -- the more direct proxy for "shared
        # infrastructure vs. sprawl" than density alone, since two cities at the same density
        # can be built very differently (small single-family lots vs. multifamily buildings)
        total_units = r.get("total_units_by_structure")
        detached = r.get("units_1detached")
        attached = r.get("units_1attached")
        if total_units:
            r["pct_single_family_detached"] = (100 * detached / total_units) if detached is not None else None
            r["pct_single_family_all"] = (
                100 * (detached + attached) / total_units
                if (detached is not None and attached is not None) else None
            )
        else:
            r["pct_single_family_detached"] = None
            r["pct_single_family_all"] = None

    # ---- write CSV ----
    fieldnames = list(rows[0].keys())
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    ok_count = sum(1 for r in rows if r.get("median_household_income") is not None)
    print(f"\nWrote {len(rows)} rows to {args.out}")
    print(f"  ACS data successfully retrieved for {ok_count}/{len(rows)} cities.")
    if ok_count < len(rows):
        print("  Cities with missing ACS data (check name/FIPS match, or place has no ACS 5-yr row):")
        for r in rows:
            if r.get("median_household_income") is None:
                print("    - " + r["name"])


if __name__ == "__main__":
    main()
