# onlineChecker

Compares the SFCC Global Online Status (GOS) export against each country's
Google Merchant Center feed, samples the products that are online but missing
from the feed, asks the Productsup stage API why, and writes a five-sheet
Excel report.

## Install

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

## Run

```bash
.venv/Scripts/python.exe main.py
```

Then: put your token in **Settings**, pick the GOS channel in **Browse**, tick
the countries in **Countries**, press **Start Comparison**.

## The country page

The reason this version exists. Previously the only way to see what was
selected was to expand project → site → channel in a lazy tree, because
checkbox state was painted when a site's channels finished loading. With 34
feeds spread over 32 sites, changing your mind cost one click per site.

The Countries tab is flat and country-first:

- one row per country, one child row per feed (a country may hold several);
- ticking a country ticks all of its feeds, and a partly-ticked country shows
  the indeterminate state;
- **Select none** / **Select all** / **Invert** act on the rows currently
  visible, so the filter box narrows what a bulk action touches;
- every change is written to `config/selection.json` immediately.

It never calls the API, so it works before a token is configured.

## Configuration

Four files, three of which you might open:

| File | Who writes it | What is in it |
|---|---|---|
| `config/settings.json` | you | four keys: `output_dir`, `max_per_country`, `sample_seed`, `download_concurrency` |
| `config/countries.json` | you | country code → the three GOS column names, plus aliases |
| `config/selection.json` | the app | GOS channel, every configured feed, and the ticked subset |
| `config/credentials.json` | you | the Productsup token |

`sample_seed` is `"random"` by default: each run draws a fresh seed, so
repeated runs check different SKUs. Set it to a number to pin the sample —
the run log always prints the seed it used, so a report can be reproduced by
copying that number back.

Timeouts, retry counts, batch sizes and chunk sizes are constants in
`core/settings.py`. They are not in the file because they were never meant to
be edited; if one ever needs overriding, add an `advanced` object to
`settings.json` with any of the keys listed in `ADVANCED_DEFAULTS`.

`config/last_session.json` from the previous version is migrated automatically
the first time the app starts, then left alone.

## Structure

```
main.py                 entry point: palette, stylesheet, icon, excepthook
core/                   business logic, no Qt imports
  settings.py           the four config files + tuning constants
  models.py             Feed / GosFeed / Selection
  feeds.py              CSV/XML readers, ID column detection (registry)
  download.py           streaming downloads with retries and cancellation
  productsup.py         platform + stage API client (StageClient interface)
  compare.py            GOS filtering, anti-join, sampling, country inference
  report.py             five-sheet workbook (sheet specs, not hardcoded calls)
  runner.py             the run: download → compare → API → report
  progress.py           ProgressUpdate phases + throttle
  redact.py             token redaction for logs and reports
ui/                     PySide6
  main_window.py        four tabs, toolbar, progress
  country_page.py       the country checklist
  browse_page.py        lazy project/site/channel tree
  results_page.py       the summary table
  workers.py            QThread wrappers
  dialogs.py            message dialog + settings
  theme.py, icons.py    stylesheet loader, inline SVG icons
styles/main.qss         stylesheet
assets/                 app icon (svg/ico/png) + stylesheet SVGs
tools/make_icon.py      regenerates the .ico from assets/app_icon.svg
tests/                  pytest suites
```

## Results

After a run the Results tab shows one row per country:

| Country | Online | Not in GMC | Skipped | Top reason | Share |

The workbook still holds everything else — sampled detail, skip-reason
distribution, API log, failed batches — for when a number needs explaining.
The Log tab keeps the run's messages.

## Tests

```bash
python tests/run_tests.py
```

`tests/test_runner.py` starts a local HTTP server that impersonates the GOS
export, two feed CSVs and the stage endpoint, so the whole pipeline is
verified without a real token. The assertions are exact counts.

## Extending

- **A new feed format**: write a `(path) -> (ids, id_column)` function and
  `feeds.register_reader("json", fn)`.
- **A new report sheet**: add a `SheetSpec` to `report.SHEETS` and pass the
  rows under that title.
- **A different stage provider**: implement `StageClient.query_batch` and add
  it to `productsup.make_client`.
- **A new summary column**: add a `(key, title, width, align)` tuple to
  `ui/results_page.COLUMNS`; the runner already emits the full row.
