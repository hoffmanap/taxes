# Density, Housing Type & the Cost of Municipal Services

Statistical analysis of population density, land area, housing type, home
values, and municipal spending/tax burden across the 150 largest U.S. cities —
built to test whether denser cities deliver municipal services more
efficiently, and to unpack why El Paso's effective property tax rate is high
despite modest home values and average-to-low density.

Originally run on the 50 largest cities; expanded to 150 specifically to get
enough statistical power to resolve a question the smaller sample could only
hint at (see finding #7 below).

## Files

| File | What it is |
|---|---|
| `density_cost_dashboard.html` | **The deliverable.** Self-contained interactive dashboard — open it in any browser, no server needed. Nine sections covering density vs. spending, housing type, service-by-service breakdown, the spending→tax mediation, and the combined home-value/housing-type story behind effective tax rates, plus an interactive multifamily-mix simulator. |
| `acs_income_housing_pull.py` | Standalone Python script that pulls median household income, median home value, median real estate taxes paid, and housing-structure-type breakdown (single-family, duplex, 3-4 units, 5-9, 10-19, 20-49, 50+) from the Census ACS 5-year API for the 150 cities. Run this yourself with a free Census API key if the underlying data ever needs refreshing. |
| `acs_income_housing_merged_150.csv` | Output of the script above — the full merged dataset (150 rows), used to build §03, §05, §06, §07 of the dashboard. |
| `base_finance_150.csv`, `service_breakdown_150.csv` | Density, land area, and 2022 Census-of-Governments finance figures (direct spending, property tax revenue, core infrastructure spending, full 16-category service breakdown) per city, before the ACS merge. |
| `category_elasticities_150.csv` | The density-elasticity regression result for each of the 16 service categories — which ones rise with density and which are flat. |
| `area_data_150.csv`, `top150_pid.pkl` | Intermediate build files (land area by city, raw finance-file parsing). Kept for reproducibility; not needed to read the dashboard. |

## Data sources

- **Population, land area, density:** U.S. Census Bureau, 2023 Gazetteer Files (places and counties)
- **Municipal spending, property tax revenue, service-category breakdown:** U.S. Census Bureau, 2022 Census of Governments — State & Local Government Finance, Individual Unit Public Use File (bulk download, no API key required)
- **Median household income, median home value, median real estate taxes paid, housing structure type:** U.S. Census Bureau, American Community Survey (ACS) 2022 5-Year Estimates, tables B19013, B25077, B25103, B25024 (pulled via `acs_income_housing_pull.py`, requires a free Census API key)

## How to re-run the ACS pull

```bash
# Get a free key: https://api.census.gov/data/key_signup.html
export CENSUS_API_KEY="your_key_here"
python acs_income_housing_pull.py
# -> writes acs_income_housing_merged_150.csv
```

Or paste the key directly into the `CENSUS_API_KEY = ""` line near the top of
the script instead of using an environment variable. The finance and land-area
figures for all 150 cities are already embedded in the script (built from
bulk Census downloads that need no key), so it only needs network access for
the ACS variables — takes a couple of minutes at the default pacing.

## Headline findings

1. **Density is associated with *more* municipal spending per resident, not less** — the opposite of the "density is efficient" hypothesis, at least in aggregate (§02). This relationship is real but noticeably weaker in the 150-city sample than it was at 50 (R² for the simplest model dropped from ~0.41 to ~0.10) — a sign the smaller, more extreme top-50 sample was overstating how tight the relationship really is.
2. **Housing type (share of single-family homes) explains spending much better than density does** (R²=0.45 vs. 0.11), and once it's in the model, density's own effect doesn't just fade — with 150 cities it actually tips slightly negative, though not quite past conventional significance (p≈0.08). Denser cities spend more mainly because they're less single-family-dominated, not because density itself is costly (§03).
3. **No individual service category gets cheaper per resident as density rises**, but the pattern is weaker across the board with more cities. Several relationships that looked strong at 50 cities — corrections most notably — are no longer statistically significant at 150, a reminder that patterns in a small, curated sample of the very largest cities don't always generalize (§04).
4. **Density's link to the tax bill is fully explained by spending, not an independent effect**, and this result replicates cleanly with three times the data. Denser cities collect more tax because they spend more, not because density itself drives tax bills (§05).
5. **The effective property tax *rate* is a combined story, with a genuine new wrinkle:** income is the dominant driver of home values (r≈0.86), and single-family housing share independently pulls home values down even after controlling for income (p<0.001, stronger than before). But with more power, housing type now also shows a small, statistically real *direct* effect on the rate once home value is held fixed (p=0.008) — running the *opposite* direction (toward a slightly lower rate), likely because single-family-heavy cities need less total revenue in the first place. That makes the value-driven story the conservative estimate, not an inflated one (§06).
6. **Quantified, and slightly stronger than before:** El Paso's home value is 22% below what its income alone would predict (was 18% at 50 cities). Removing just that single-family-driven gap (holding income's effect and its actual tax bill fixed) would cut its effective rate from 2.17% to 1.70% — a 22% reduction, closing 43% of the entire gap between El Paso and the average large city's rate. In this data, lowering the tax rate and raising service levels are not a trade-off — they trace back to the same underlying shift in taxable value per acre (§06).
7. **Does the type of multifamily matter? This is now resolved, not just hinted at.** This was the specific reason the sample was expanded from 50 to 150 cities — the smaller sample saw the point estimates differ (large multifamily > missing-middle) but didn't have the power to confirm it was real. With 147 cities, both structure sizes are now clearly, independently significant for spending (p<0.0001 each) and for home value (p=0.002 and p<0.0001) — and a direct test of whether they differ from each other is *still* not significant (p=0.51 for spending, p=0.25 for home value), now with real power behind that null result. Missing-middle housing (2-9 units) is a confirmed lever, not just an unproven alternative to large apartment buildings, for both service delivery and taxable value (§06).
8. **In plain numbers:** at El Paso's actual housing-stock size (~260,000 units), 25,000 new multifamily units — about 9.6% growth, a realistic multi-year rezoning target — would cut the effective rate by roughly 5.6% and save the owner of a median-priced home about $194/year. Smaller, more immediately achievable increments (1,000–10,000 units) produce real but modest savings ($9–$83/year) — the relationship is gradual and cumulative by nature, not a single-project fix (§06).

## Known limitations

- Three consolidated city-county governments (Louisville/Jefferson County, Honolulu, Nashville-Davidson) are missing from the ACS-derived sections (§03, §06) because their FIPS geography doesn't have a matching ACS place-level record. They're present everywhere else (density, spending, service categories). 11 cities total are flagged as consolidated governments — the original 7 plus Anchorage, Baton Rouge, and two in Georgia (Augusta, Columbus) — detected automatically by name pattern (see the script's docstring for detection caveats, including why Jacksonville and Indianapolis are *not* flagged despite the "consolidated" label in common usage).
- "Effective tax rate" is a city-wide median-on-median ratio (median taxes paid ÷ median home value), not an average of individual households' actual rates — solid for cross-city comparison, won't match any single homeowner's bill exactly.
- Per-capita spending is used throughout as a proxy for service level. This is a reasonable proxy but not a perfect one — it doesn't distinguish higher service levels from lower efficiency (paying more for the same service). §04's service-by-service breakdown is the closest this analysis gets to separating the two.
- Expanding the sample further (beyond the top 150) would likely start to hurt rather than help: ACS margin of error grows quickly for smaller places, government structure gets much more heterogeneous (more of what a big city self-provides gets pushed onto counties/special districts in smaller ones), and the analysis would start pooling qualitatively different kinds of cities without a lot more controls. The top 100–150 range was chosen as roughly the point where more data still helps.
- Two pairs of cities in the 150 share a name (Aurora, CO/IL; Glendale, AZ/CA) — both real, correctly kept as distinct rows, but worth knowing if extending any lookup-by-name logic.

Compiled and analyzed September 2026.
