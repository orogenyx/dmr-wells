# dmr-wells

Daily snapshot of the North Dakota DMR well list for a handful of operators, as JSON.

The ND Department of Mineral Resources well search at
<https://www.dmr.nd.gov/oilgas/findwellsvw.asp> is a POST form with no API and no cross-origin
access, so a web page cannot query it directly. A scheduled GitHub Action in this repo posts the
form once per operator, saves the result table as JSON under `wells/`, and commits it. The
Job Paperwork app then reads those files over plain HTTPS.

**The data is ND DMR public record.** Nothing here is private; this repo only reformats what the
DMR search page shows to anyone.

## Files

```
operators.json              input: the operators to pull, edited by hand
fetch_wells.py              the fetcher (Python 3, standard library only)
.github/workflows/fetch.yml daily schedule + manual run
wells/index.json            output: one entry per operator with well count and fetch time
wells/<operator-slug>.json  output: one file per operator
```

## Adding an operator

1. Open <https://www.dmr.nd.gov/oilgas/findwellsvw.asp> and copy the operator's name **exactly as
   it appears in the Operator dropdown** (case, commas and periods included). The fetcher checks the
   name against the dropdown and skips it with a warning if it does not match.
2. Add it to `operators.json`:
   ```json
   { "operators": ["Phoenix Operating LLC", "Another Operator, LLC"] }
   ```
3. Commit and push, then either wait for the next scheduled run or open the **Actions** tab,
   choose **Fetch ND DMR wells**, and press **Run workflow**.

The file name is the operator name lower-cased with runs of non-alphanumerics replaced by one
hyphen: `Phoenix Operating LLC` → `wells/phoenix-operating-llc.json`. To drop an operator, remove it
from `operators.json` and delete its file under `wells/`.

## Data format

`wells/index.json`:

```json
{
  "generated": "2026-11-10T12:00:12Z",
  "operators": [
    { "operator": "Phoenix Operating LLC", "slug": "phoenix-operating-llc",
      "file": "wells/phoenix-operating-llc.json", "wells": 293, "fetched": "2026-11-10T12:00:12Z" }
  ]
}
```

`wells/<slug>.json`:

```json
{
  "operator": "Phoenix Operating LLC",
  "fetched": "2026-11-10T12:00:12Z",
  "source": "https://www.dmr.nd.gov/oilgas/findwellsvw.asp",
  "wells": [
    {
      "file": "42988",
      "api": "33-023-01818",
      "well": "CANDY FERRARI 24-25-36 2H",
      "operator": "Phoenix Operating LLC",
      "status": "DRL",
      "location": "SWSW 13-160-98",
      "county": "DIVIDE",
      "field": "SKABO",
      "footages": "",
      "latitude": null,
      "longitude": null
    }
  ]
}
```

- `file` is the DMR file number. `api` is the 10-digit ND API number with dashes, `33-CCC-NNNNN`,
  or `""` when the DMR shows none.
- `well` is the name exactly as the DMR lists it (most are upper-case, a few are mixed case).
  `field` is upper-cased. `location` is quarter-quarter then section-township-range, e.g.
  `SWSW 13-160-98`.
- `status` is the DMR well status code (`DRL` drilling, `A` active, `LOC` permitted location,
  `PA` plugged and abandoned, `IA` inactive, `DRY`, `PNC`, `PNS`, `Confidential`, ...).
- `county` is derived from the county code inside the API number (ND uses FIPS county codes, so
  `33-023-…` is Divide); it is `""` when there is no API number. The search page itself has no county
  column.
- `footages`, `latitude` and `longitude` are not returned by the search, so they stay `""` / `null`.
- Every well the search returns is kept, including plugged and permitted ones, sorted by well name.

## How the fetcher behaves

- One GET of the form (to read the operator dropdown), then one POST per operator with a 2 s pause,
  a 30 s timeout and a User-Agent naming this repo. No retry loop; a failed operator is logged as a
  workflow warning, its previous file is kept, and the run still succeeds so the other operators
  update. The run fails only when every operator failed.
- The result table is parsed by header name (`File No`, `API No`, `Well Status`, `Location`,
  `Operator`, `Well Name`, `Field`). If the DMR changes the page layout the parser raises instead of
  silently filling the wrong column.
- Files are rewritten wholesale on every run, but the Action commits `wells/` only when a well
  actually changed (a new, renamed, re-statused or dropped well). A run whose only difference is
  the `fetched` / `generated` timestamps is discarded with "No well changes." So `fetched` in the
  committed files is the "data as of" date: the fetch that last found a change. The well list is
  still checked daily; the date just does not move while nothing changes.
- Schedule: 12:00 UTC daily (06:00 Central Standard Time, 07:00 Central Daylight Time).

Run it by hand with Python 3.8+ and no extra packages:

```bash
python fetch_wells.py
python fetch_wells.py --operator "Phoenix Operating LLC"
```

## How the Job Paperwork app uses it

The app holds the raw base URL of this repo, `https://raw.githubusercontent.com/<github-user>/dmr-wells/main/`.
When it is online it fetches `wells/index.json`, picks the operator matching the job's operator
name, fetches that operator's file, finds the current well by name, and fills in API, Field and
Location, tagged as coming from the DMR with the `fetched` date shown. Offline it uses the last
copy it cached and the manual "Find API on ND DMR" button still works.
