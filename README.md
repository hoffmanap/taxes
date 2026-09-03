# Density, Housing Type, and the Cost of Municipal Services

A statistical analysis of population density, land area, housing type, home
values, business density, and municipal spending and tax burden across the
150 largest U.S. cities. Built to test whether denser cities deliver
municipal services more efficiently, and to understand why El Paso's
effective property tax rate is high despite modest home values and average
to below-average density.

## Files

| File | What it is |
|---|---|
| `density_cost_dashboard.html` | The deliverable. Self-contained interactive dashboard, open it in any browser, no server needed. Eleven sections covering density and spending, housing type, service-by-service breakdown, the spending-to-tax relationship, the combined home value and housing type story behind effective tax rates, business density, real commercial and multifamily tax base data for El Paso and its Texas peers, and an interactive multifamily-mix simulator. |
| `acs_income_housing_pull.py` | Standalone Python script that pulls median household income, median home value, median real estate taxes paid, and housing structure type (single-family, duplex, 3 to 4 units, 5 to 9, 10 to 19, 20 to 49, 50 or more) from the Census ACS 5-year API for all 150 cities. Requires a free Census API key. |
| `jobs_density_pull.py` | Standalone Python script that pulls total workplace employment by city from the Census Bureau's LEHD LODES data and computes jobs density. No API key required, the data is public bulk files. |
| `acs_income_housing_merged_150.csv` | Output of the ACS script, one row per city, used throughout the dashboard. |
| `jobs_density_150.csv` | Output of the jobs density script, one row per city. |
| `tx_all_cities_final.csv` | Property value by category (single-family, multifamily, commercial, industrial, and other) for all 18 Texas cities in the sample, across 12 counties, pulled from the Texas Comptroller's Appraisal District Ratio Study. |
| `base_finance_150.csv`, `service_breakdown_150.csv` | Density, land area, and 2022 Census of Governments finance figures (direct spending, property tax revenue, core infrastructure spending, and the full 16-category service breakdown) per city. |
| `category_elasticities_150.csv` | The density elasticity regression result for each of the 16 service categories, which ones scale up with density and which are flat. |

## Data sources

- **Population, land area, density:** U.S. Census Bureau, 2023 Gazetteer Files
- **Municipal spending, property tax revenue, service category breakdown:** U.S. Census Bureau, 2022 Census of Governments, State and Local Government Finance, Individual Unit Public Use File (bulk download, no key required)
- **Median household income, median home value, median real estate taxes paid, housing structure type:** U.S. Census Bureau, American Community Survey 2022 5-Year Estimates, tables B19013, B25077, B25103, B25024 (requires a free Census API key)
- **Business density:** U.S. Census Bureau, LEHD Origin-Destination Employment Statistics (LODES), 2021 (bulk download, no key required)
- **Commercial and multifamily property value:** Texas Comptroller of Public Accounts, Appraisal District Ratio Study, for El Paso, Austin, Dallas, Fort Worth, Houston, and San Antonio

## How to re-run the data pulls

```bash
# ACS pull: get a free key at https://api.census.gov/data/key_signup.html
export CENSUS_API_KEY="your_key_here"
python acs_income_housing_pull.py
# writes acs_income_housing_merged_150.csv

# Jobs density pull: no key needed
python jobs_density_pull.py
# writes jobs_density_150.csv
```

The ACS script can also take the key directly: paste it into the
`CENSUS_API_KEY = ""` line near the top of the file instead of using an
environment variable. Both scripts have the finance and land area figures
for all 150 cities already embedded, built from bulk Census downloads, so
they only need network access for their own data pulls.

## Headline findings

1. **Density is associated with more municipal spending per resident, not less.** This is the opposite of the idea that denser cities deliver services more efficiently, at least in aggregate. Per-capita spending is treated throughout as a proxy for the level of service a resident receives, not simply a cost (§01, §02).

2. **Housing type explains spending better than density does.** The share of a city's housing that's single-family detached explains 45% of the city-to-city variation in spending, versus 11% for density alone. Once housing type is in the model, density's own effect is not just weaker, it actually turns slightly negative. Denser cities spend more mainly because they're less single-family-dominated, not because density itself is costly (§03).

3. **No individual service category gets cheaper per resident as density rises.** Some categories, sewers, parks, roads, trash, and health, are flat. The rest, especially corrections, parking, libraries, housing programs, and general administration, get more expensive per resident as density rises (§04).

4. **Density's link to the tax bill runs through spending, not around it.** Denser cities collect more tax because they spend more, not because density itself drives tax bills independently (§05).

5. **The effective property tax rate is a combined story.** Income is the dominant driver of home values (r = 0.90), but single-family housing share independently pulls home values down even after controlling for income (p < 0.001). Since the rate is mechanically tax revenue divided by home value, low-value housing stock forces a higher rate to raise the same money. Holding home value fixed, single-family share actually shows a small, separate relationship with a slightly lower rate, likely because single-family-heavy cities need less total revenue in the first place (§06).

6. **Quantified for El Paso:** its home value is 22% below what its income alone would predict. Removing just that single-family-driven gap, while holding its actual income effect and tax bill fixed, would cut its effective rate from 2.17% to 1.70%, a 22% reduction, closing 43% of the entire gap between El Paso and the average large city's rate (§06).

7. **Does the type of multifamily housing matter?** No. Missing-middle housing (2 to 9 unit buildings) and large apartment buildings (10 or more units) have statistically indistinguishable effects on both spending (p = 0.51) and home value (p = 0.25). Missing-middle infill is a confirmed, productive lever in its own right, not an unproven alternative to large apartment construction, and it is typically the easier political and physical lift in existing single-family neighborhoods (§06).

8. **In plain numbers:** at El Paso's actual housing stock size, about 260,000 units, 25,000 new multifamily units (roughly 9.6% growth, a realistic multi-year rezoning target) would cut the effective rate by roughly 5.6% and save the owner of a median-priced home about $194 a year. Smaller increments produce real but modest savings, the relationship is gradual and cumulative, not a single-project fix (§06).

9. **Business density behaves the same way population density does.** Jobs per square mile moves closely with population density (r = 0.86) and predicts spending about as well on its own, but adds nothing once housing type is in the model. More importantly, jobs density does not lower a homeowner's effective tax rate. Holding home value constant, more jobs nearby is associated with a higher rate, not a lower one. El Paso's jobs-per-resident ratio ranks 108th of 146 cities, below the sample median (§07).

10. **Real property tax records tell a consistent, more modest version of the housing-type story.** Pulled directly from the Texas Comptroller for every Texas city in the sample, 18 cities across 12 counties: El Paso's commercial and industrial share of taxable value (27.6%) is not unusually low, it sits in the middle of the pack. What is unusually low is El Paso's multifamily share of taxable value (5.0%), the lowest of any county in the comparison except Laredo and Amarillo, both smaller and less dense than El Paso. Across all 12 counties, multifamily share correlates negatively with the effective tax rate (r = -0.50) more than commercial share does (r = -0.08), but this broader comparison does not reach conventional statistical significance (p = 0.10). Restricting the same comparison to just the six largest Texas cities shows a stronger, significant correlation (r = -0.85, p = 0.03). The honest read: multifamily share is consistently the stronger and more plausible lever compared to commercial share, but the Texas comparison alone is not large enough to call the relationship confirmed on its own (§08).

## Known limitations

- Three consolidated city-county governments (Louisville/Jefferson County, Honolulu, and Nashville-Davidson) are missing from the ACS-derived sections (§03, §06) because their FIPS geography doesn't have a matching ACS place-level record. They're present everywhere else. Anchorage is present in most sections but absent from the jobs density section because its LODES data hasn't been updated since 2016.
- Eleven cities are flagged as consolidated governments, detected by name pattern (New York, San Francisco, Denver, Washington D.C., Honolulu, Nashville, Louisville, Anchorage, Baton Rouge, and two in Georgia). Jacksonville and Indianapolis are commonly called consolidated city-county governments but are not flagged here, because Census reports their finances as separate city and county units.
- Comparing service categories across a 47-fold range of city sizes (175,000 to 8.25 million people) has a real limit: the same category label, such as "Libraries," does not guarantee the same kind of institution at every city size. Smaller, newer, more single-family-dominated cities may simply never have built the same breadth of civic infrastructure that older, larger cities have, often by design rather than by underfunding. Population size and single-family share are significantly correlated (r = -0.34, p < 0.0001), confirming this is a real pattern in the data, not a hypothetical concern. The claim that more taxable value and more tax revenue would follow from a housing-mix shift is well supported. The claim that this revenue would fund the same type of expanded civic services that larger legacy cities provide is a plausible but less certain extension of it.
- Jobs density measures economic activity, not commercial property value. A city can have many jobs without much of that activity showing up as local commercial tax base. Section 8's Texas Comptroller data measures the tax base directly and is the more precise source on that specific question, but covers only the 18 Texas cities in the sample rather than all 150, because property classification is not standardized nationally. Within that Texas comparison, the multifamily-to-tax-rate relationship is directionally consistent but does not reach conventional statistical significance at 18 cities (12 counties), so it should be read as a supporting data point alongside §03 and §06's larger, better-powered findings, not as independent confirmation on its own.
- "Effective tax rate" throughout is a city-wide median-on-median ratio (median real estate taxes paid divided by median home value), not an average of individual households' actual rates. It is a solid basis for comparing cities to each other, but will not match any single homeowner's bill exactly.
- Per-capita spending is used throughout as a proxy for service level. This is a reasonable proxy but not a perfect one, it does not distinguish a higher level of service from lower efficiency at delivering the same service. The service-by-service breakdown in §04 is the closest this analysis gets to separating the two.

Compiled and analyzed September 2026.
