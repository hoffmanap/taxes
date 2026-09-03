#!/usr/bin/env python3
"""
acs_income_housing_pull.py

Fetches median household income, median home value, median real-estate taxes
paid, and household/housing counts for the 150 largest U.S. cities from the
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
    - share of housing by structure size (duplex, 3-4, 5-9, 10-19, 20-49, 50+ units) and
      two convenience groupings: "missing middle" (2-9 units) vs. "large multifamily" (10+ units)

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
The density/land-area/expenditure/tax-revenue figures for all 150 cities are
embedded below (BASE_DATA) exactly as compiled from the 2022 Census of
Governments bulk files -- no key needed for those, they were already pulled.
This script only needs to fetch the ACS variables that weren't previously
available: median household income, median home value, median real estate
taxes paid, and household/housing-unit counts.

KNOWN CAVEATS IN THE EXPANDED (150-CITY) DATASET
--------------------------------------------------
- consolidated_govt is detected automatically by name pattern (e.g. "AND
  COUNTY", "CONSOLIDATED", "METROPOLITAN GOVERNMENT", "CITY-PARISH",
  "MUNICIPALITY") plus two manual exceptions for New York City and
  Washington D.C., which don't follow those naming patterns but are
  structurally unique. This is best-effort, not exhaustive -- it correctly
  catches the original 7 (NYC, SF, Denver, DC, Honolulu, Nashville,
  Louisville) plus 4 more that appear in the expanded range (Anchorage,
  Baton Rouge, Augusta GA, Columbus GA). Jacksonville and Indianapolis are
  NOT flagged despite being commonly described as "consolidated" city-county
  governments -- Census's own finance reporting keeps their city and county
  figures as separate government units, so this dataset's finance numbers
  for those two are city-only, consistent with how Census itself organizes
  the data. Spot-check this flag if you extend the list further.
- Two pairs of cities share a name (Aurora, CO vs. Aurora, IL; Glendale, AZ
  vs. Glendale, CA) -- both are real, distinct cities correctly kept as
  separate rows (different gid/state_fips), but any downstream code that
  looks cities up by name alone needs to disambiguate by state.
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
#   B25024_002E  1, detached (the "classic" single-family home)
#   B25024_003E  1, attached (townhomes/rowhouses -- still single-family in form,
#                just sharing a wall; usually counted as "single-family" in housing analyses)
#   B25024_004E  2 units (a duplex)
#   B25024_005E  3 or 4 units
#   B25024_006E  5 to 9 units
#   B25024_007E  10 to 19 units
#   B25024_008E  20 to 49 units
#   B25024_009E  50 or more units
#   B25024_010E  Mobile home
#   B25024_011E  Boat, RV, van, etc.
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
    "units_2": "B25024_004E",
    "units_3to4": "B25024_005E",
    "units_5to9": "B25024_006E",
    "units_10to19": "B25024_007E",
    "units_20to49": "B25024_008E",
    "units_50plus": "B25024_009E",
    "units_mobile_home": "B25024_010E",
    "units_boat_rv_van": "B25024_011E",
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
        "gid": "202173201810",
        "name": "WICHITA CITY",
        "state_fips": "20",
        "place_fips": "79000",
        "pop": 391731,
        "land_area_sqmi": 162.941,
        "density": 2404.13,
        "direct_gen_exp_pc": 1066.31,
        "property_tax_pc": 376.89,
        "core_infra_exp_pc": 689.06,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "222071166094",
        "name": "NEW ORLEANS CITY",
        "state_fips": "22",
        "place_fips": "55000",
        "pop": 389476,
        "land_area_sqmi": 169.5,
        "density": 2297.79,
        "direct_gen_exp_pc": 4595.14,
        "property_tax_pc": 878.53,
        "core_infra_exp_pc": 1507.98,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "082005194644",
        "name": "AURORA CITY",
        "state_fips": "08",
        "place_fips": "04000",
        "pop": 387377,
        "land_area_sqmi": 163.009,
        "density": 2376.41,
        "direct_gen_exp_pc": 1827.53,
        "property_tax_pc": 316.01,
        "core_infra_exp_pc": 903.57,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062029183825",
        "name": "BAKERSFIELD CITY",
        "state_fips": "06",
        "place_fips": "03526",
        "pop": 385725,
        "land_area_sqmi": 150.273,
        "density": 2566.83,
        "direct_gen_exp_pc": 1456.34,
        "property_tax_pc": 249.67,
        "core_infra_exp_pc": 820.2,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "392035172358",
        "name": "CLEVELAND CITY",
        "state_fips": "39",
        "place_fips": "16000",
        "pop": 378589,
        "land_area_sqmi": 77.735,
        "density": 4870.25,
        "direct_gen_exp_pc": 2049.97,
        "property_tax_pc": 103.22,
        "core_infra_exp_pc": 1210.52,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "062059183836",
        "name": "ANAHEIM CITY",
        "state_fips": "06",
        "place_fips": "02000",
        "pop": 353676,
        "land_area_sqmi": 50.283,
        "density": 7033.71,
        "direct_gen_exp_pc": 2087.63,
        "property_tax_pc": 267.72,
        "core_infra_exp_pc": 1141.97,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062059100711",
        "name": "SANTA ANA CITY",
        "state_fips": "06",
        "place_fips": "69000",
        "pop": 331301,
        "land_area_sqmi": 27.376,
        "density": 12101.88,
        "direct_gen_exp_pc": 2481.91,
        "property_tax_pc": 279.93,
        "core_infra_exp_pc": 845.97,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062065207594",
        "name": "RIVERSIDE CITY",
        "state_fips": "06",
        "place_fips": "62000",
        "pop": 330786,
        "land_area_sqmi": 81.149,
        "density": 4076.28,
        "direct_gen_exp_pc": 1348.94,
        "property_tax_pc": 225.13,
        "core_infra_exp_pc": 920.98,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "322003169987",
        "name": "HENDERSON CITY",
        "state_fips": "32",
        "place_fips": "31900",
        "pop": 329172,
        "land_area_sqmi": 120.523,
        "density": 2731.2,
        "direct_gen_exp_pc": 1478.73,
        "property_tax_pc": 375.51,
        "core_infra_exp_pc": 878.02,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "482355176325",
        "name": "CORPUS CHRISTI CITY",
        "state_fips": "48",
        "place_fips": "17000",
        "pop": 327248,
        "land_area_sqmi": 162.282,
        "density": 2016.54,
        "direct_gen_exp_pc": 1437.62,
        "property_tax_pc": 432.26,
        "core_infra_exp_pc": 856.02,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "212067165831",
        "name": "LEXINGTON-FAYETTE URBAN COUNTY GOVERNMENT",
        "state_fips": "21",
        "place_fips": "46027",
        "pop": 324735,
        "land_area_sqmi": 283.855,
        "density": 1144.02,
        "direct_gen_exp_pc": 1605.46,
        "property_tax_pc": 363.36,
        "core_infra_exp_pc": 817.81,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "062077211214",
        "name": "STOCKTON CITY",
        "state_fips": "06",
        "place_fips": "75000",
        "pop": 312716,
        "land_area_sqmi": 63.095,
        "density": 4956.27,
        "direct_gen_exp_pc": 1509.36,
        "property_tax_pc": 231.58,
        "core_infra_exp_pc": 990.77,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "272123167605",
        "name": "ST PAUL CITY",
        "state_fips": "27",
        "place_fips": "58000",
        "pop": 306717,
        "land_area_sqmi": 51.973,
        "density": 5901.47,
        "direct_gen_exp_pc": 2341.39,
        "property_tax_pc": 623.37,
        "core_infra_exp_pc": 1349.79,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "392061191166",
        "name": "CINCINNATI CITY",
        "state_fips": "39",
        "place_fips": "15000",
        "pop": 304548,
        "land_area_sqmi": 77.914,
        "density": 3908.77,
        "direct_gen_exp_pc": 2090.77,
        "property_tax_pc": 242.13,
        "core_infra_exp_pc": 1143.32,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "422003133470",
        "name": "PITTSBURGH CITY",
        "state_fips": "42",
        "place_fips": "61000",
        "pop": 299226,
        "land_area_sqmi": 55.378,
        "density": 5403.34,
        "direct_gen_exp_pc": 2487.79,
        "property_tax_pc": 550.2,
        "core_infra_exp_pc": 1305.41,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "372081171207",
        "name": "GREENSBORO CITY",
        "state_fips": "37",
        "place_fips": "28000",
        "pop": 297878,
        "land_area_sqmi": 133.861,
        "density": 2225.28,
        "direct_gen_exp_pc": 1847.62,
        "property_tax_pc": 608.54,
        "core_infra_exp_pc": 1340.84,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "292510169159",
        "name": "ST LOUIS CITY",
        "state_fips": "29",
        "place_fips": "65000",
        "pop": 297645,
        "land_area_sqmi": 61.72,
        "density": 4822.5,
        "direct_gen_exp_pc": 3451.93,
        "property_tax_pc": 386.69,
        "core_infra_exp_pc": 1537.35,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "482085176053",
        "name": "PLANO CITY",
        "state_fips": "48",
        "place_fips": "58016",
        "pop": 291296,
        "land_area_sqmi": 71.699,
        "density": 4062.76,
        "direct_gen_exp_pc": 1737.05,
        "property_tax_pc": 720.53,
        "core_infra_exp_pc": 926.49,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "312109169646",
        "name": "LINCOLN CITY",
        "state_fips": "31",
        "place_fips": "28000",
        "pop": 290505,
        "land_area_sqmi": 99.97,
        "density": 2905.92,
        "direct_gen_exp_pc": 1379.8,
        "property_tax_pc": 292.5,
        "core_infra_exp_pc": 683.2,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "122095161665",
        "name": "ORLANDO CITY",
        "state_fips": "12",
        "place_fips": "53000",
        "pop": 289457,
        "land_area_sqmi": 111.236,
        "density": 2602.19,
        "direct_gen_exp_pc": 3943.42,
        "property_tax_pc": 835.53,
        "core_infra_exp_pc": 2094.1,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "022020194463",
        "name": "ANCHORAGE MUNICIPALITY",
        "state_fips": "02",
        "place_fips": "03000",
        "pop": 287095,
        "land_area_sqmi": 1707.004,
        "density": 168.19,
        "direct_gen_exp_pc": 6259.22,
        "property_tax_pc": 2134.69,
        "core_infra_exp_pc": 1477.42,
        "consolidated_govt": 1,
        "region": "West"
    },
    {
        "gid": "372063208963",
        "name": "DURHAM CITY",
        "state_fips": "37",
        "place_fips": "19000",
        "pop": 285897,
        "land_area_sqmi": 116.754,
        "density": 2448.71,
        "direct_gen_exp_pc": 1574.21,
        "property_tax_pc": 759.38,
        "core_infra_exp_pc": 863.07,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "062059161229",
        "name": "IRVINE CITY",
        "state_fips": "06",
        "place_fips": "36770",
        "pop": 283700,
        "land_area_sqmi": 65.613,
        "density": 4323.84,
        "direct_gen_exp_pc": 1613.67,
        "property_tax_pc": 313.17,
        "core_infra_exp_pc": 766.23,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "342013130835",
        "name": "NEWARK CITY",
        "state_fips": "34",
        "place_fips": "51000",
        "pop": 282520,
        "land_area_sqmi": 24.144,
        "density": 11701.46,
        "direct_gen_exp_pc": 2941.95,
        "property_tax_pc": 892.45,
        "core_infra_exp_pc": 1356.14,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "062073211212",
        "name": "CHULA VISTA CITY",
        "state_fips": "06",
        "place_fips": "13392",
        "pop": 272979,
        "land_area_sqmi": 49.638,
        "density": 5499.4,
        "direct_gen_exp_pc": 1575.65,
        "property_tax_pc": 249.63,
        "core_infra_exp_pc": 674.92,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "182003163465",
        "name": "FORT WAYNE CITY",
        "state_fips": "18",
        "place_fips": "25000",
        "pop": 272398,
        "land_area_sqmi": 110.628,
        "density": 2462.29,
        "direct_gen_exp_pc": 1077.93,
        "property_tax_pc": 579.77,
        "core_infra_exp_pc": 753.17,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "392095205133",
        "name": "TOLEDO CITY",
        "state_fips": "39",
        "place_fips": "77000",
        "pop": 271455,
        "land_area_sqmi": 80.488,
        "density": 3372.61,
        "direct_gen_exp_pc": 639.08,
        "property_tax_pc": 49.99,
        "core_infra_exp_pc": 179.45,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "122103161693",
        "name": "ST PETERSBURG CITY",
        "state_fips": "12",
        "place_fips": "63000",
        "pop": 267802,
        "land_area_sqmi": 61.804,
        "density": 4333.09,
        "direct_gen_exp_pc": 2240.75,
        "property_tax_pc": 555.25,
        "core_infra_exp_pc": 1304.54,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "042013100249",
        "name": "CHANDLER CITY",
        "state_fips": "04",
        "place_fips": "12000",
        "pop": 265398,
        "land_area_sqmi": 65.639,
        "density": 4043.3,
        "direct_gen_exp_pc": 1437.81,
        "property_tax_pc": 148.99,
        "core_infra_exp_pc": 827.95,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "482479135293",
        "name": "LAREDO CITY",
        "state_fips": "48",
        "place_fips": "41464",
        "pop": 263640,
        "land_area_sqmi": 106.824,
        "density": 2467.98,
        "direct_gen_exp_pc": 1633.57,
        "property_tax_pc": 378.16,
        "core_infra_exp_pc": 1065.98,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "552025209777",
        "name": "MADISON CITY",
        "state_fips": "55",
        "place_fips": "48000",
        "pop": 263094,
        "land_area_sqmi": 83.088,
        "density": 3166.45,
        "direct_gen_exp_pc": 1958.43,
        "property_tax_pc": 1068.49,
        "core_infra_exp_pc": 1207.5,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "342017193734",
        "name": "JERSEY CITY CITY",
        "state_fips": "34",
        "place_fips": "36000",
        "pop": 262664,
        "land_area_sqmi": 14.748,
        "density": 17810.14,
        "direct_gen_exp_pc": 2753.66,
        "property_tax_pc": 812.64,
        "core_infra_exp_pc": 948.39,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "042013189585",
        "name": "SCOTTSDALE CITY",
        "state_fips": "04",
        "place_fips": "65000",
        "pop": 262647,
        "land_area_sqmi": 184.003,
        "density": 1427.41,
        "direct_gen_exp_pc": 2151.64,
        "property_tax_pc": 277.53,
        "core_infra_exp_pc": 1137.57,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "482303212233",
        "name": "LUBBOCK CITY",
        "state_fips": "48",
        "place_fips": "45000",
        "pop": 262611,
        "land_area_sqmi": 141.961,
        "density": 1849.88,
        "direct_gen_exp_pc": 1317.8,
        "property_tax_pc": 402.9,
        "core_infra_exp_pc": 678.25,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "322003208734",
        "name": "NORTH LAS VEGAS CITY",
        "state_fips": "32",
        "place_fips": "51800",
        "pop": 260098,
        "land_area_sqmi": 105.319,
        "density": 2469.62,
        "direct_gen_exp_pc": 1124.44,
        "property_tax_pc": 336.88,
        "core_infra_exp_pc": 645.35,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "322031169993",
        "name": "RENO CITY",
        "state_fips": "32",
        "place_fips": "60600",
        "pop": 259290,
        "land_area_sqmi": 108.993,
        "density": 2378.96,
        "direct_gen_exp_pc": 1569.98,
        "property_tax_pc": 356.62,
        "core_infra_exp_pc": 895.3,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "042013160831",
        "name": "GILBERT TOWN",
        "state_fips": "04",
        "place_fips": "27400",
        "pop": 257658,
        "land_area_sqmi": 68.663,
        "density": 3752.5,
        "direct_gen_exp_pc": 1336.74,
        "property_tax_pc": 117.82,
        "core_infra_exp_pc": 819.88,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "042013207535",
        "name": "GLENDALE CITY",
        "state_fips": "04",
        "place_fips": "27820",
        "pop": 255307,
        "land_area_sqmi": 67.126,
        "density": 3803.4,
        "direct_gen_exp_pc": 1612.53,
        "property_tax_pc": 142.67,
        "core_infra_exp_pc": 1020.5,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "362029186634",
        "name": "BUFFALO CITY",
        "state_fips": "36",
        "place_fips": "11000",
        "pop": 254479,
        "land_area_sqmi": 40.379,
        "density": 6302.26,
        "direct_gen_exp_pc": 6324.55,
        "property_tax_pc": 316.45,
        "core_infra_exp_pc": 1266.95,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "372067194744",
        "name": "WINSTON-SALEM CITY",
        "state_fips": "37",
        "place_fips": "75000",
        "pop": 248112,
        "land_area_sqmi": 133.584,
        "density": 1857.35,
        "direct_gen_exp_pc": 1265.05,
        "property_tax_pc": 653.93,
        "core_infra_exp_pc": 811.39,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "512550114563",
        "name": "CHESAPEAKE CITY",
        "state_fips": "51",
        "place_fips": "16000",
        "pop": 247011,
        "land_area_sqmi": 338.456,
        "density": 729.82,
        "direct_gen_exp_pc": 4639.33,
        "property_tax_pc": 1665.64,
        "core_infra_exp_pc": 1087.3,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "512710136057",
        "name": "NORFOLK CITY",
        "state_fips": "51",
        "place_fips": "57000",
        "pop": 242803,
        "land_area_sqmi": 53.275,
        "density": 4557.54,
        "direct_gen_exp_pc": 5295.07,
        "property_tax_pc": 1485.24,
        "core_infra_exp_pc": 1142.43,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "482113176079",
        "name": "IRVING CITY",
        "state_fips": "48",
        "place_fips": "37000",
        "pop": 240916,
        "land_area_sqmi": 66.979,
        "density": 3596.89,
        "direct_gen_exp_pc": 1527.45,
        "property_tax_pc": 777.85,
        "core_infra_exp_pc": 963.0,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "482113212231",
        "name": "GARLAND CITY",
        "state_fips": "48",
        "place_fips": "29000",
        "pop": 238139,
        "land_area_sqmi": 56.857,
        "density": 4188.38,
        "direct_gen_exp_pc": 1722.6,
        "property_tax_pc": 561.94,
        "core_infra_exp_pc": 781.5,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "062001207577",
        "name": "FREMONT CITY",
        "state_fips": "06",
        "place_fips": "26000",
        "pop": 234569,
        "land_area_sqmi": 78.103,
        "density": 3003.33,
        "direct_gen_exp_pc": 1511.38,
        "property_tax_pc": 516.24,
        "core_infra_exp_pc": 1057.22,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "512760194178",
        "name": "RICHMOND CITY",
        "state_fips": "51",
        "place_fips": "67000",
        "pop": 232226,
        "land_area_sqmi": 59.925,
        "density": 3875.28,
        "direct_gen_exp_pc": 6147.33,
        "property_tax_pc": 1941.61,
        "core_infra_exp_pc": 1622.98,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "122086161604",
        "name": "HIALEAH CITY",
        "state_fips": "12",
        "place_fips": "30000",
        "pop": 232027,
        "land_area_sqmi": 21.581,
        "density": 10751.45,
        "direct_gen_exp_pc": 1288.79,
        "property_tax_pc": 325.56,
        "core_infra_exp_pc": 1006.33,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "162001162035",
        "name": "BOISE CITY",
        "state_fips": "16",
        "place_fips": "08830",
        "pop": 229776,
        "land_area_sqmi": 84.553,
        "density": 2717.54,
        "direct_gen_exp_pc": 1963.48,
        "property_tax_pc": 674.6,
        "core_infra_exp_pc": 1064.25,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "532063205599",
        "name": "SPOKANE CITY",
        "state_fips": "53",
        "place_fips": "67000",
        "pop": 222050,
        "land_area_sqmi": 68.76,
        "density": 3229.35,
        "direct_gen_exp_pc": 2213.79,
        "property_tax_pc": 398.45,
        "core_infra_exp_pc": 1407.47,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "532053176868",
        "name": "TACOMA CITY",
        "state_fips": "53",
        "place_fips": "70000",
        "pop": 219945,
        "land_area_sqmi": 49.714,
        "density": 4424.21,
        "direct_gen_exp_pc": 2864.96,
        "property_tax_pc": 380.01,
        "core_infra_exp_pc": 1763.8,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "222033166054",
        "name": "BATON ROUGE-EAST BATON ROUGE CITY-PARISH",
        "state_fips": "22",
        "place_fips": "05000",
        "pop": 219052,
        "land_area_sqmi": 86.943,
        "density": 2519.49,
        "direct_gen_exp_pc": 4925.82,
        "property_tax_pc": 1160.05,
        "core_infra_exp_pc": 2006.79,
        "consolidated_govt": 1,
        "region": "South"
    },
    {
        "gid": "062071193115",
        "name": "SAN BERNARDINO CITY",
        "state_fips": "06",
        "place_fips": "65000",
        "pop": 217491,
        "land_area_sqmi": 62.1,
        "density": 3502.27,
        "direct_gen_exp_pc": 1110.74,
        "property_tax_pc": 92.22,
        "core_infra_exp_pc": 801.41,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062071161245",
        "name": "FONTANA CITY",
        "state_fips": "06",
        "place_fips": "24680",
        "pop": 216173,
        "land_area_sqmi": 43.278,
        "density": 4994.99,
        "direct_gen_exp_pc": 1541.6,
        "property_tax_pc": 381.99,
        "core_infra_exp_pc": 978.97,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062099207610",
        "name": "MODESTO CITY",
        "state_fips": "06",
        "place_fips": "48354",
        "pop": 215666,
        "land_area_sqmi": 43.358,
        "density": 4974.08,
        "direct_gen_exp_pc": 1434.45,
        "property_tax_pc": 177.65,
        "core_infra_exp_pc": 962.91,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062065161238",
        "name": "MORENO VALLEY CITY",
        "state_fips": "06",
        "place_fips": "49270",
        "pop": 212349,
        "land_area_sqmi": 51.31,
        "density": 4138.55,
        "direct_gen_exp_pc": 986.88,
        "property_tax_pc": 235.58,
        "core_infra_exp_pc": 364.39,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "192153208056",
        "name": "DES MOINES CITY",
        "state_fips": "19",
        "place_fips": "21000",
        "pop": 212312,
        "land_area_sqmi": 88.162,
        "density": 2408.2,
        "direct_gen_exp_pc": 2627.05,
        "property_tax_pc": 846.51,
        "core_infra_exp_pc": 1660.09,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "372051131717",
        "name": "FAYETTEVILLE CITY",
        "state_fips": "37",
        "place_fips": "22920",
        "pop": 211705,
        "land_area_sqmi": 148.266,
        "density": 1427.87,
        "direct_gen_exp_pc": 1644.41,
        "property_tax_pc": 369.83,
        "core_infra_exp_pc": 966.74,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "062037137232",
        "name": "SANTA CLARITA CITY",
        "state_fips": "06",
        "place_fips": "69088",
        "pop": 209990,
        "land_area_sqmi": 73.614,
        "density": 2852.58,
        "direct_gen_exp_pc": 942.47,
        "property_tax_pc": 257.1,
        "core_infra_exp_pc": 460.66,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "482085194820",
        "name": "FRISCO CITY",
        "state_fips": "48",
        "place_fips": "27684",
        "pop": 209980,
        "land_area_sqmi": 68.587,
        "density": 3061.51,
        "direct_gen_exp_pc": 1662.46,
        "property_tax_pc": 725.05,
        "core_infra_exp_pc": 936.27,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "122111161710",
        "name": "PORT ST LUCIE CITY",
        "state_fips": "12",
        "place_fips": "58715",
        "pop": 209715,
        "land_area_sqmi": 119.216,
        "density": 1759.12,
        "direct_gen_exp_pc": 1287.71,
        "property_tax_pc": 320.06,
        "core_infra_exp_pc": 401.04,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "482085187646",
        "name": "MCKINNEY CITY",
        "state_fips": "48",
        "place_fips": "45744",
        "pop": 208272,
        "land_area_sqmi": 66.963,
        "density": 3110.25,
        "direct_gen_exp_pc": 1766.09,
        "property_tax_pc": 617.21,
        "core_infra_exp_pc": 745.47,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "062111161299",
        "name": "OXNARD CITY",
        "state_fips": "06",
        "place_fips": "54652",
        "pop": 207945,
        "land_area_sqmi": 26.545,
        "density": 7833.68,
        "direct_gen_exp_pc": 2476.3,
        "property_tax_pc": 376.68,
        "core_infra_exp_pc": 946.35,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "012073225038",
        "name": "BIRMINGHAM CITY",
        "state_fips": "01",
        "place_fips": "07000",
        "pop": 206950,
        "land_area_sqmi": 147.07,
        "density": 1407.15,
        "direct_gen_exp_pc": 2455.24,
        "property_tax_pc": 371.16,
        "core_infra_exp_pc": 1376.84,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "362055131239",
        "name": "ROCHESTER CITY",
        "state_fips": "36",
        "place_fips": "63000",
        "pop": 205225,
        "land_area_sqmi": 35.761,
        "density": 5738.79,
        "direct_gen_exp_pc": 7436.56,
        "property_tax_pc": 541.69,
        "core_infra_exp_pc": 1348.07,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "492035176492",
        "name": "SALT LAKE CITY CITY",
        "state_fips": "49",
        "place_fips": "67000",
        "pop": 204087,
        "land_area_sqmi": 110.891,
        "density": 1840.43,
        "direct_gen_exp_pc": 5735.14,
        "property_tax_pc": 742.58,
        "core_infra_exp_pc": 1373.91,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "012089207520",
        "name": "HUNTSVILLE CITY",
        "state_fips": "01",
        "place_fips": "37000",
        "pop": 202964,
        "land_area_sqmi": 223.633,
        "density": 907.58,
        "direct_gen_exp_pc": 2485.53,
        "property_tax_pc": 376.26,
        "core_infra_exp_pc": 890.85,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "122071161649",
        "name": "CAPE CORAL CITY",
        "state_fips": "12",
        "place_fips": "10275",
        "pop": 200972,
        "land_area_sqmi": 106.123,
        "density": 1893.76,
        "direct_gen_exp_pc": 1884.28,
        "property_tax_pc": 547.2,
        "core_infra_exp_pc": 981.84,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "042013160833",
        "name": "TEMPE CITY",
        "state_fips": "04",
        "place_fips": "73000",
        "pop": 200402,
        "land_area_sqmi": 39.981,
        "density": 5012.43,
        "direct_gen_exp_pc": 2423.02,
        "property_tax_pc": 296.84,
        "core_infra_exp_pc": 1214.1,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "362119109593",
        "name": "YONKERS CITY",
        "state_fips": "36",
        "place_fips": "84000",
        "pop": 200040,
        "land_area_sqmi": 18.006,
        "density": 11109.63,
        "direct_gen_exp_pc": 6284.82,
        "property_tax_pc": 1285.92,
        "core_infra_exp_pc": 1221.62,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "262081185901",
        "name": "GRAND RAPIDS CITY",
        "state_fips": "26",
        "place_fips": "34000",
        "pop": 200031,
        "land_area_sqmi": 44.776,
        "density": 4467.37,
        "direct_gen_exp_pc": 2197.34,
        "property_tax_pc": 391.14,
        "core_infra_exp_pc": 949.62,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "482375194809",
        "name": "AMARILLO CITY",
        "state_fips": "48",
        "place_fips": "03000",
        "pop": 199654,
        "land_area_sqmi": 103.615,
        "density": 1926.88,
        "direct_gen_exp_pc": 1688.07,
        "property_tax_pc": 288.47,
        "core_infra_exp_pc": 816.02,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "062059100709",
        "name": "HUNTINGTON BEACH CITY",
        "state_fips": "06",
        "place_fips": "36000",
        "pop": 198246,
        "land_area_sqmi": 26.982,
        "density": 7347.34,
        "direct_gen_exp_pc": 1742.97,
        "property_tax_pc": 515.51,
        "core_infra_exp_pc": 1084.79,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "052119100435",
        "name": "LITTLE ROCK CITY",
        "state_fips": "05",
        "place_fips": "41000",
        "pop": 197866,
        "land_area_sqmi": 120.281,
        "density": 1645.03,
        "direct_gen_exp_pc": 2740.5,
        "property_tax_pc": 322.63,
        "core_infra_exp_pc": 1176.03,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "062037161170",
        "name": "GLENDALE CITY",
        "state_fips": "06",
        "place_fips": "30000",
        "pop": 197747,
        "land_area_sqmi": 30.479,
        "density": 6487.98,
        "direct_gen_exp_pc": 2512.77,
        "property_tax_pc": 393.85,
        "core_infra_exp_pc": 1261.92,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "132245194679",
        "name": "AUGUSTA-RICHMOND COUNTY CONSOLIDATED GOVERNMENT",
        "state_fips": "13",
        "place_fips": "04204",
        "pop": 197468,
        "land_area_sqmi": 302.282,
        "density": 653.26,
        "direct_gen_exp_pc": 2152.48,
        "property_tax_pc": 405.24,
        "core_infra_exp_pc": 1095.11,
        "consolidated_govt": 1,
        "region": "South"
    },
    {
        "gid": "202091208111",
        "name": "OVERLAND PARK CITY",
        "state_fips": "20",
        "place_fips": "53775",
        "pop": 197381,
        "land_area_sqmi": 75.218,
        "density": 2624.12,
        "direct_gen_exp_pc": 1267.74,
        "property_tax_pc": 292.33,
        "core_infra_exp_pc": 571.1,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "132215161944",
        "name": "COLUMBUS CONSOLIDATED GOVERNMENT",
        "state_fips": "13",
        "place_fips": "19000",
        "pop": 196442,
        "land_area_sqmi": 216.5,
        "density": 907.35,
        "direct_gen_exp_pc": 2003.01,
        "property_tax_pc": 488.65,
        "core_infra_exp_pc": 915.53,
        "consolidated_govt": 1,
        "region": "South"
    },
    {
        "gid": "172089189929",
        "name": "AURORA CITY",
        "state_fips": "17",
        "place_fips": "03012",
        "pop": 196383,
        "land_area_sqmi": 45.051,
        "density": 4359.13,
        "direct_gen_exp_pc": 1259.23,
        "property_tax_pc": 460.97,
        "core_infra_exp_pc": 848.63,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "122073161650",
        "name": "TALLAHASSEE CITY",
        "state_fips": "12",
        "place_fips": "70600",
        "pop": 196326,
        "land_area_sqmi": 102.428,
        "density": 1916.72,
        "direct_gen_exp_pc": 3214.04,
        "property_tax_pc": 256.74,
        "core_infra_exp_pc": 1418.77,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "012101100091",
        "name": "MONTGOMERY CITY",
        "state_fips": "01",
        "place_fips": "51000",
        "pop": 196268,
        "land_area_sqmi": 159.857,
        "density": 1227.77,
        "direct_gen_exp_pc": 1037.76,
        "property_tax_pc": 177.78,
        "core_infra_exp_pc": 751.02,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "392153209096",
        "name": "AKRON CITY",
        "state_fips": "39",
        "place_fips": "01000",
        "pop": 195994,
        "land_area_sqmi": 61.933,
        "density": 3164.61,
        "direct_gen_exp_pc": 2433.14,
        "property_tax_pc": 132.12,
        "core_infra_exp_pc": 1327.42,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "482113194823",
        "name": "GRAND PRAIRIE CITY",
        "state_fips": "48",
        "place_fips": "30464",
        "pop": 195272,
        "land_area_sqmi": 72.512,
        "density": 2692.96,
        "direct_gen_exp_pc": 2099.11,
        "property_tax_pc": 582.9,
        "core_infra_exp_pc": 1082.16,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "472093175780",
        "name": "KNOXVILLE CITY",
        "state_fips": "47",
        "place_fips": "40000",
        "pop": 190223,
        "land_area_sqmi": 98.732,
        "density": 1926.66,
        "direct_gen_exp_pc": 2493.03,
        "property_tax_pc": 687.26,
        "core_infra_exp_pc": 1161.88,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "462099184196",
        "name": "SIOUX FALLS CITY",
        "state_fips": "46",
        "place_fips": "59020",
        "pop": 187809,
        "land_area_sqmi": 82.671,
        "density": 2271.76,
        "direct_gen_exp_pc": 1654.98,
        "property_tax_pc": 395.88,
        "core_infra_exp_pc": 832.49,
        "consolidated_govt": 0,
        "region": "Midwest"
    },
    {
        "gid": "012097160683",
        "name": "MOBILE CITY",
        "state_fips": "01",
        "place_fips": "50000",
        "pop": 187746,
        "land_area_sqmi": 139.482,
        "density": 1346.02,
        "direct_gen_exp_pc": 1728.36,
        "property_tax_pc": 148.19,
        "core_infra_exp_pc": 994.38,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "532011176816",
        "name": "VANCOUVER CITY",
        "state_fips": "53",
        "place_fips": "74060",
        "pop": 186192,
        "land_area_sqmi": 48.748,
        "density": 3819.48,
        "direct_gen_exp_pc": 1389.64,
        "property_tax_pc": 314.83,
        "core_infra_exp_pc": 986.91,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "222017127810",
        "name": "SHREVEPORT CITY",
        "state_fips": "22",
        "place_fips": "70000",
        "pop": 184786,
        "land_area_sqmi": 108.145,
        "density": 1708.69,
        "direct_gen_exp_pc": 1871.42,
        "property_tax_pc": 314.43,
        "core_infra_exp_pc": 1189.18,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "472065191559",
        "name": "CHATTANOOGA CITY",
        "state_fips": "47",
        "place_fips": "14000",
        "pop": 184742,
        "land_area_sqmi": 142.352,
        "density": 1297.78,
        "direct_gen_exp_pc": 2841.46,
        "property_tax_pc": 1001.02,
        "core_infra_exp_pc": 1623.71,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "252027106079",
        "name": "WORCESTER CITY",
        "state_fips": "25",
        "place_fips": "82000",
        "pop": 184570,
        "land_area_sqmi": 37.36,
        "density": 4940.31,
        "direct_gen_exp_pc": 5386.89,
        "property_tax_pc": 1970.35,
        "core_infra_exp_pc": 964.27,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "122011161585",
        "name": "FORT LAUDERDALE CITY",
        "state_fips": "12",
        "place_fips": "24000",
        "pop": 184245,
        "land_area_sqmi": 34.582,
        "density": 5327.77,
        "direct_gen_exp_pc": 3231.52,
        "property_tax_pc": 941.74,
        "core_infra_exp_pc": 1692.89,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "482061176022",
        "name": "BROWNSVILLE CITY",
        "state_fips": "48",
        "place_fips": "10768",
        "pop": 183428,
        "land_area_sqmi": 131.796,
        "density": 1391.76,
        "direct_gen_exp_pc": 1160.57,
        "property_tax_pc": 283.39,
        "core_infra_exp_pc": 568.68,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "062071161246",
        "name": "ONTARIO CITY",
        "state_fips": "06",
        "place_fips": "53896",
        "pop": 183393,
        "land_area_sqmi": 49.984,
        "density": 3669.03,
        "direct_gen_exp_pc": 3706.44,
        "property_tax_pc": 429.35,
        "core_infra_exp_pc": 1763.27,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "042013160832",
        "name": "PEORIA CITY",
        "state_fips": "04",
        "place_fips": "54050",
        "pop": 179872,
        "land_area_sqmi": 176.697,
        "density": 1017.97,
        "direct_gen_exp_pc": 1460.64,
        "property_tax_pc": 174.55,
        "core_infra_exp_pc": 1037.81,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "442007112811",
        "name": "PROVIDENCE CITY",
        "state_fips": "44",
        "place_fips": "59000",
        "pop": 179270,
        "land_area_sqmi": 18.406,
        "density": 9739.76,
        "direct_gen_exp_pc": 5012.91,
        "property_tax_pc": 2007.79,
        "core_infra_exp_pc": 1246.96,
        "consolidated_govt": 0,
        "region": "Northeast"
    },
    {
        "gid": "512700176784",
        "name": "NEWPORT NEWS CITY",
        "state_fips": "51",
        "place_fips": "56000",
        "pop": 179062,
        "land_area_sqmi": 68.996,
        "density": 2595.25,
        "direct_gen_exp_pc": 5881.61,
        "property_tax_pc": 1716.13,
        "core_infra_exp_pc": 1259.98,
        "consolidated_govt": 0,
        "region": "South"
    },
    {
        "gid": "062071161251",
        "name": "RANCHO CUCAMONGA CITY",
        "state_fips": "06",
        "place_fips": "59451",
        "pop": 178849,
        "land_area_sqmi": 46.51,
        "density": 3845.39,
        "direct_gen_exp_pc": 1427.86,
        "property_tax_pc": 781.83,
        "core_infra_exp_pc": 946.87,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062067200728",
        "name": "ELK GROVE CITY",
        "state_fips": "06",
        "place_fips": "22020",
        "pop": 177302,
        "land_area_sqmi": 42.601,
        "density": 4161.92,
        "direct_gen_exp_pc": 983.25,
        "property_tax_pc": 175.13,
        "core_infra_exp_pc": 605.44,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "412047205218",
        "name": "SALEM CITY",
        "state_fips": "41",
        "place_fips": "64900",
        "pop": 175891,
        "land_area_sqmi": 48.931,
        "density": 3594.67,
        "direct_gen_exp_pc": 1901.72,
        "property_tax_pc": 635.79,
        "core_infra_exp_pc": 1007.96,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062073161256",
        "name": "OCEANSIDE CITY",
        "state_fips": "06",
        "place_fips": "53322",
        "pop": 174648,
        "land_area_sqmi": 41.268,
        "density": 4232.04,
        "direct_gen_exp_pc": 1847.46,
        "property_tax_pc": 426.9,
        "core_infra_exp_pc": 1126.87,
        "consolidated_govt": 0,
        "region": "West"
    },
    {
        "gid": "062097161287",
        "name": "SANTA ROSA CITY",
        "state_fips": "06",
        "place_fips": "70098",
        "pop": 174613,
        "land_area_sqmi": 42.536,
        "density": 4105.06,
        "direct_gen_exp_pc": 1730.9,
        "property_tax_pc": 285.73,
        "core_infra_exp_pc": 1201.69,
        "consolidated_govt": 0,
        "region": "West"
    }
]
''')


def main():
    parser = argparse.ArgumentParser(description="Pull ACS income/home-value data for the 150 largest U.S. cities.")
    parser.add_argument("--key", default=None,
                         help="Census API key. If omitted, falls back to the CENSUS_API_KEY "
                              "environment variable, then to the CENSUS_API_KEY value hardcoded "
                              "near the top of this file. Get a free key at "
                              "https://api.census.gov/data/key_signup.html")
    parser.add_argument("--out", default="acs_income_housing_merged_150.csv",
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

        # granular breakdown of the multifamily share by structure size -- lets you test
        # whether "missing middle" (2-4 units) behaves differently from larger apartment
        # buildings (20+ units), rather than treating all non-single-family housing as one bucket
        size_buckets = {
            "pct_units_2": "units_2",
            "pct_units_3to4": "units_3to4",
            "pct_units_5to9": "units_5to9",
            "pct_units_10to19": "units_10to19",
            "pct_units_20to49": "units_20to49",
            "pct_units_50plus": "units_50plus",
            "pct_units_mobile_home": "units_mobile_home",
            "pct_units_boat_rv_van": "units_boat_rv_van",
        }
        for pct_key, raw_key in size_buckets.items():
            raw_val = r.get(raw_key)
            r[pct_key] = (100 * raw_val / total_units) if (total_units and raw_val is not None) else None

        # convenience groupings for the "which scale of multifamily matters" question:
        #   missing middle = duplex through 9-unit buildings (fits on single-family-scale lots)
        #   large multifamily = 10+ units (apartment-building scale)
        if total_units:
            missing_middle_units = sum(
                r.get(k) or 0 for k in ("units_2", "units_3to4", "units_5to9")
            )
            large_mf_units = sum(
                r.get(k) or 0 for k in ("units_10to19", "units_20to49", "units_50plus")
            )
            r["pct_missing_middle"] = 100 * missing_middle_units / total_units
            r["pct_large_multifamily"] = 100 * large_mf_units / total_units
        else:
            r["pct_missing_middle"] = None
            r["pct_large_multifamily"] = None

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
