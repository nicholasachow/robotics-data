# Physical AI data stack

A living map of who manufactures training data for robots and world models: collectors, curation platforms, simulation, and capture hardware. Static page, no framework.

Live: https://nicholasachow.com/robotics-data/

## How it updates

```
Google Sheet ──Master tab (all columns, private notes)
             ├─Config tab (column → public/private, page_key)
             ├─Lists tab (canonical modalities / region / tier / buyers; headers = Master column names)
             └─Public tab (FILTER: Config-public columns, rows where public = TRUE)
                    │
                    ▼  uv run build.py [--push]
                data.js  ──▶  index.html  ──▶  GitHub Pages
```

- `index.html` is the page. It loads `data.js` and renders the supply-chain diagram and the filterable table.
- `data.js` is generated. Do not edit it by hand.
- `build.py` reads the Config, Lists and Public tabs, refuses to write if a private column appears, validates tier/modality/region values against Lists, requires `http(s)://` URLs, and checks that Lists tiers match the four stage cards hard-coded in `index.html`. It writes `data.js`; if no record or list changed, the file is byte-identical and the `updated` date stays put. With `--push` it commits (message names companies added, removed, changed) and pushes, and Pages redeploys.
- `favicon.svg` is the NC mark; it flips to light on dark tab bars.
- Filters can be preset in the URL: `?tier=Collector&mod=Tactile&region=China&q=glove`.

```
uv run build.py            # write data.js
uv run build.py --dry-run  # preview, touch nothing
uv run build.py --push     # write, commit, push
```

## Data model

One row per company. Which columns are public is set in the sheet's Config tab; the page currently renders `name`, `zh`, `sub`, `tier`, `mods`, `region`, `hq`, `capture`, `sample`, `funding`, `founders`, `notes`. `mods` and `region` are `;`-separated lists in the sheet.

Tiers: `Collector`, `Curation & aggregation`, `Simulation & synthetic`, `Capture hardware`.

Format credit: [Pavlov's List](https://pavlovslist.com/robotics-data) by Chris Barber.
