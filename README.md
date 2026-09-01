# Density, Housing Type & the Cost of Municipal Services

Statistical analysis of population density, land area, housing type, home
values, and municipal spending/tax burden across the 50 largest U.S. cities —
built to test whether denser cities deliver municipal services more
efficiently, and to unpack why El Paso's effective property tax rate is high
despite modest home values and average-to-low density.

## Files

| File | What it is |
|---|---|
| `density_cost_dashboard.html` | **The deliverable.** Self-contained interactive dashboard — open it in any browser, no server needed. Nine sections covering density vs. spending, housing type, service-by-service breakdown, the spending→tax mediation, and the combined home-value/housing-type story behind effective tax rates. |
| `acs_income_housing_pull.py` | Standalone Python script that pulls median household income, median home value, median real estate taxes paid, and housing-structure-type (single-family share) from the Census ACS 5-year API for the 50 cities. Run this yourself with a free Census API key if the underlying data ever needs refreshing. |
| `acs_income_housing_merged.csv` | Output of the script above — the full merged dataset (50 rows), used to build §03, §05, §06, §07 of the dashboard. |
| `master_dataset.csv` | Density, land area, and 2022 Census-of-Governments finance figures (direct spending, property tax revenue, core infrastructure spending) per city, before the ACS merge. |
| `service_breakdown.csv` | Per-city per-capita spending broken into the 16 service categories used in §04 (police, fire, highways, sewerage, etc.). |
| `category_elasticities.csv` | The density-elasticity regression result for each of the 16 service categories — which ones rise with density and which are flat. |
| `area_data.csv`, `fips_lookup.csv`, `finance_processed.csv` | Intermediate build files (land area by city, FIPS code lookup, raw finance data before merging). Kept for reproducibility; not needed to read the dashboard. |

## Data sources

- **Population, land area, density:** U.S. Census Bureau, 2023 Gazetteer Files (places and counties)
- **Municipal spending, property tax revenue, service-category breakdown:** U.S. Census Bureau, 2022 Census of Governments — State & Local Government Finance, Individual Unit Public Use File (bulk download, no API key required)
- **Median household income, median home value, median real estate taxes paid, housing structure type:** U.S. Census Bureau, American Community Survey (ACS) 2022 5-Year Estimates, tables B19013, B25077, B25103, B25024 (pulled via `acs_income_housing_pull.py`, requires a free Census API key)

## How to re-run the ACS pull

```bash
# Get a free key: https://api.census.gov/data/key_signup.html
export CENSUS_API_KEY="your_key_here"
python acs_income_housing_pull.py
# -> writes acs_income_housing_merged.csv
```

Or paste the key directly into the `CENSUS_API_KEY = ""` line near the top of
the script instead of using an environment variable. The finance and land-area
figures for all 50 cities are already embedded in the script, so it only
needs network access for the ACS variables.

## Headline findings

1. **Density is associated with *more* municipal spending per resident, not less** — the opposite of the "density is efficient" hypothesis, at least in aggregate (§02).
2. **Housing type (share of single-family homes) explains spending better than density does**, and once it's in the model, density stops mattering on its own. Denser cities spend more mainly because they're less single-family-dominated, not because density itself is costly (§03).
3. **No individual service category gets cheaper per resident as density rises.** Some (sewers, parks, roads, trash, health) are flat; the rest — especially corrections, parking, libraries, housing programs, and general administration — get more expensive per resident as density rises (§04).
4. **Density's link to the tax bill is fully explained by spending, not an independent effect.** Denser cities collect more tax because they spend more, not because density itself drives tax bills (§05).
5. **The effective property tax *rate* (as opposed to the bill) is a combined story:** income is the dominant driver of home values (r=0.90), but single-family housing share independently pulls home values down even after controlling for income (p=0.001). Since the rate is mechanically tax revenue ÷ home value, low-value housing stock forces a higher rate to raise the same money. El Paso combines low income, a heavy single-family share, and consequently a home value 18% below what its income alone would predict — landing it the 2nd-highest effective tax rate in the sample while its actual per-capita spending (service level) is 4th-lowest of 47, just 49% of the median (§06).

## Known limitations

- Three consolidated city-county governments (Louisville/Jefferson County, Honolulu, Nashville-Davidson) are missing from the ACS-derived sections (§03, §06) because their FIPS geography doesn't have a matching ACS place-level record. They're present everywhere else (density, spending, service categories).
- Consolidated city-county governments generally (NYC, San Francisco, Denver, Washington D.C., plus the three above) carry county-level service responsibilities most peer cities don't, which inflates their spending figures independent of density. They're flagged throughout and excluded from several robustness checks (see §07).
- "Effective tax rate" is a city-wide median-on-median ratio (median taxes paid ÷ median home value), not an average of individual households' actual rates — solid for cross-city comparison, won't match any single homeowner's bill exactly.
- Per-capita spending is used throughout as a proxy for service level. This is a reasonable proxy but not a perfect one — it doesn't distinguish higher service levels from lower efficiency (paying more for the same service). §04's service-by-service breakdown is the closest this analysis gets to separating the two.

Compiled and analyzed September 2026.
