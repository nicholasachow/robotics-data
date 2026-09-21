# /// script
# requires-python = ">=3.11"
# dependencies = ["google-auth>=2.30", "google-auth-oauthlib>=1.2", "requests>=2.32"]
# ///
"""
Pull the Public tab of the "Physical AI data stack" Google Sheet and write data.js
for the static page.

Column visibility lives in the sheet's Config tab (column | visibility | page_key | notes).
The Public tab's formulas expose only columns marked public, so the sheet is safe
on its own. This script adds a tripwire: it also reads Config and refuses to write
if any header marked private shows up in the Public output.

The Lists tab holds the canonical option lists, one per column, in display order.
Its headers equal the Master/Config column names (modalities | region | tier | buyers …)
and each list is exported under that column's Config page_key (mods | region | tier);
a header with no Config row (e.g. buyers) is exported under its own name.

Checks that stop the build:
  - tier list must equal PAGE_TIERS exactly (index.html hard-codes those stages)
  - every row's tier / modalities / region values must appear in the Lists tab
  - url and sample_url must start with http:// or https:// when present
  - no private column may appear in the Public tab

If the records and lists are unchanged from the existing data.js, META.updated is
kept so the file is byte-identical and --push reports "no changes to publish".

    uv run build.py            # write data.js, print a summary
    uv run build.py --push     # also git commit + push (GitHub Pages redeploys)
    uv run build.py --dry-run  # print what would be written, touch nothing
    uv run build.py --no-vault # skip regenerating the vault mirror table

Auth reuses the Google Sheets MCP token at ~/.config/google-sheets-token.json
(read-only use of a spreadsheets scope). Override with SHEETS_TOKEN_PATH.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SPREADSHEET_ID = "1CmgAi5g-MHl3ADgVGuv5DRyLj0DaRYWCjSHxchYooIg"
PUBLIC_RANGE = "Public!A:Z"
CONFIG_RANGE = "Config!A:C"
LISTS_RANGE = "Lists!A:Z"  # headers = Master column names (modalities | region | tier | buyers …), values in display order
TOKEN_PATH = Path(os.environ.get("SHEETS_TOKEN_PATH", "~/.config/google-sheets-token.json")).expanduser()
HERE = Path(__file__).resolve().parent
OUT = HERE / "data.js"
VAULT_MIRROR = Path("~/Documents/obsidian_vault/!Physical AI data supply chain.md").expanduser()

LIST_FIELDS = {"mods", "region"}  # page keys whose cells hold multi-select chips (", " separated; ";" tolerated)
REQUIRED_LIST_KEYS = ("mods", "region", "tier")  # page keys the Lists tab must supply (validation + filters)
URL_FIELDS = ("url", "sample_url")  # page keys that must be absolute http(s) links when present

# index.html hard-codes stage cards and colors for exactly these four tiers, in this order.
# If the sheet's tier list drifts, the page must be updated too, so the build refuses.
PAGE_TIERS = ["Collector", "Curation & aggregation", "Simulation & synthetic", "Capture hardware"]


def creds() -> Credentials:
    if not TOKEN_PATH.exists():
        sys.exit(f"token not found at {TOKEN_PATH}; run the google-sheets MCP once to create it")
    c = Credentials.from_authorized_user_file(str(TOKEN_PATH))
    if not c.valid:
        c.refresh(Request())
    return c


def fetch(rng: str, token: str) -> list[list[str]]:
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{rng}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json().get("values", [])


def read_config(rows: list[list[str]]) -> tuple[dict[str, str], set[str], dict[str, str]]:
    """Return (header -> page_key for public columns, set of private headers, header -> page_key for ALL columns).

    The third mapping is used to route Lists headers to page keys regardless of visibility;
    a column with no page_key maps to its own name.
    """
    public, private, page_keys = {}, set(), {}
    for row in rows[1:]:
        row = row + [""] * (3 - len(row))
        col, vis, key = (x.strip() for x in row[:3])
        if not col:
            continue
        page_keys[col] = key or col
        if vis == "public":
            public[col] = key or col
        else:
            private.add(col)  # anything not explicitly public is private
    if not public:
        sys.exit("Config tab has no public columns")
    return public, private, page_keys


def read_lists(rows: list[list[str]], page_keys: dict[str, str]) -> dict[str, list[str]]:
    """Lists tab -> {page_key: [values in sheet order]}.

    Each header is routed through Config's page_key when the header is a Config column
    (public or private); otherwise the header itself is the key. So
    modalities -> mods, region -> region, tier -> tier, buyers -> buyers.
    """
    if not rows:
        sys.exit("Lists tab is empty; it should hold modalities | region | tier | buyers")
    header = [h.strip() for h in rows[0]]
    out: dict[str, list[str]] = {}
    for i, h in enumerate(header):
        if not h:
            continue
        key = page_keys.get(h, h)
        if key in out:
            sys.exit(f"Lists tab: headers {h!r} and another column both map to page key {key!r}")
        out[key] = [r[i].strip() for r in rows[1:] if len(r) > i and r[i].strip()]
    for k in REQUIRED_LIST_KEYS:
        if not out.get(k):
            sys.exit(f"Lists tab has no values for {k} (headers found: {header})")
    return out


def check_tiers(lists: dict[str, list[str]]) -> None:
    """The page hard-codes PAGE_TIERS; refuse if the sheet's tier list disagrees."""
    sheet, page = set(lists["tier"]), set(PAGE_TIERS)
    if sheet == page:
        return
    msg = ["Lists!tier does not match the tiers hard-coded in index.html (PAGE_TIERS)."]
    if page - sheet:
        msg.append(f"  missing from the sheet: {sorted(page - sheet)}")
    if sheet - page:
        msg.append(f"  extra in the sheet:     {sorted(sheet - page)}")
    msg.append("  Update index.html and PAGE_TIERS together, or fix the Lists tab.")
    sys.exit("\n".join(msg))


def split_chips(v: str) -> list[str]:
    return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]


def to_records(rows: list[list[str]], columns: dict[str, str], private: set[str], lists: dict[str, list[str]]) -> list[dict]:
    if not rows:
        sys.exit("Public tab returned no rows")
    header = [h.strip() for h in rows[0]]
    leaked = [h for h in header if h in private]
    if leaked:
        sys.exit(f"TRIPWIRE: private columns present in the Public tab: {leaked}. Check the Public tab formulas.")
    unmapped = [h for h in header if h not in columns]
    if unmapped:
        print(f"note: Public tab has columns with no Config entry, passing them through as-is: {unmapped}")
    records, problems = [], []
    for i, row in enumerate(rows[1:], start=2):
        row = row + [""] * (len(header) - len(row))
        rec = {}
        for h, v in zip(header, row):
            k = columns.get(h, h)
            v = (v or "").strip()
            rec[k] = split_chips(v) if k in LIST_FIELDS else v
        if not rec.get("name"):
            continue
        for k in LIST_FIELDS:
            rec.setdefault(k, [])
        if rec.get("tier") not in lists["tier"]:
            problems.append(f"row {i} {rec['name']!r}: tier {rec.get('tier')!r} not in Lists!tier {lists['tier']}")
        for k in LIST_FIELDS:
            for v in rec.get(k, []):
                if v not in lists[k]:
                    problems.append(f"row {i} {rec['name']!r}: {k} value {v!r} not in the Lists tab")
        for k in URL_FIELDS:
            v = (rec.get(k) or "").strip()
            rec[k] = v
            if v and not v.startswith(("http://", "https://")):
                problems.append(f"row {i} {rec['name']!r}: {k} {v!r} must start with http:// or https://")
        records.append({k: v for k, v in rec.items() if v not in ("", [])})
    if problems:
        sys.exit("fix these in the sheet first:\n  " + "\n  ".join(problems))
    return records


def parse_data_js(text: str) -> tuple[dict, dict, list] | None:
    """Parse META, LISTS and DATA out of data.js text. Return None on any parse failure."""
    try:
        def payload(prefix: str) -> str:
            i = text.index(prefix) + len(prefix)
            j = text.find("\nwindow.", i)  # next top-level assignment, or end of file
            chunk = text[i:] if j == -1 else text[i:j]
            return chunk.strip().rstrip(";").strip()

        meta = json.loads(payload("window.META = "))
        lists = json.loads(payload("window.LISTS = "))
        data = json.loads(payload("window.DATA = "))
        if not isinstance(meta, dict) or not isinstance(lists, dict) or not isinstance(data, list):
            return None
        return meta, lists, data
    except Exception:
        return None


def read_existing(path: Path) -> tuple[dict, dict, list] | None:
    """Parse the existing data.js on disk. Return None if absent or unparsable (treated as changed)."""
    if not path.exists():
        return None
    parsed = parse_data_js(path.read_text(encoding="utf-8"))
    if parsed is None:
        print(f"note: could not parse existing {path.name}; treating as changed")
    return parsed


def _names(recs: list[dict]) -> list[str]:
    return [r.get("name", "") for r in recs if r.get("name")]


def _fmt_names(names: list[str], limit: int = 15) -> str:
    names = sorted(names, key=str.lower)
    if len(names) <= limit:
        return ", ".join(names)
    return ", ".join(names[:limit]) + f" … (+{len(names) - limit} more)"


def commit_message(new: list[dict], old: list[dict] | None, updated: str) -> str:
    lines = [f"Publish list: {len(new)} companies ({updated})"]
    old = old or []
    new_by, old_by = {r["name"]: r for r in new}, {r["name"]: r for r in old}
    added = [n for n in new_by if n not in old_by]
    removed = [n for n in old_by if n not in new_by]
    changed = [n for n in new_by if n in old_by and new_by[n] != old_by[n]]
    body = []
    if added:
        body.append(f"Added: {_fmt_names(added)}")
    if removed:
        body.append(f"Removed: {_fmt_names(removed)}")
    if changed:
        body.append(f"Changed: {_fmt_names(changed)}")
    if body:
        lines += [""] + body
    return "\n".join(lines)


def md_cell(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip() or "—"


def write_vault_mirror(records: list[dict], updated: str, tier_order: list[str]) -> None:
    """Regenerate the table section of the vault reference note from the public records."""
    if not VAULT_MIRROR.exists():
        print("note: vault mirror not found, skipping")
        return
    text = VAULT_MIRROR.read_text(encoding="utf-8")
    start, end = text.find("### Table"), text.find("### Buyers")
    if start == -1 or end == -1:
        print("note: vault mirror lacks '### Table' / '### Buyers' markers, skipping")
        return
    rows = sorted(records, key=lambda r: (tier_order.index(r["tier"]), r["name"].lower()))
    lines = ["### Table", "", "| Company | Tier | Modality | HQ / capture | Sample or dataset | Notes |", "|---|---|---|---|---|---|"]
    for r in rows:
        name = r["name"] + (f" {r['zh']}" if r.get("zh") else "")
        name = f"[{name}]({r['url']})" if r.get("url") else name
        sample = r.get("sample", "")
        if sample and r.get("sample_url"):
            sample = f"[{sample}]({r['sample_url']})"
        hq = "; ".join(x for x in [r.get("hq", ""), r.get("capture", "")] if x)
        lines.append(f"| {md_cell(name)} | {md_cell(r['tier'])} | {md_cell(', '.join(r.get('mods', [])))} | {md_cell(hq)} | {md_cell(sample)} | {md_cell(r.get('notes', ''))} |")
    new = "\n".join(lines) + "\n\n"
    text = text[:start] + new + text[end:]
    text = re.sub(r"Last updated: \d{4}-\d{2}-\d{2}", f"Last updated: {updated}", text)
    VAULT_MIRROR.write_text(text, encoding="utf-8")
    print(f"mirrored {len(rows)} rows to {VAULT_MIRROR.name}")


def main() -> None:
    args = set(sys.argv[1:])
    token = creds().token
    columns, private, page_keys = read_config(fetch(CONFIG_RANGE, token))
    lists = read_lists(fetch(LISTS_RANGE, token), page_keys)
    check_tiers(lists)
    records = to_records(fetch(PUBLIC_RANGE, token), columns, private, lists)

    # No-op rebuild: keep the old META.updated when nothing but the date would change,
    # so data.js stays byte-identical and --push sees nothing to commit.
    today = date.today().isoformat()
    existing = read_existing(OUT)
    old_meta, old_lists, old_data = existing if existing else ({}, None, None)
    if existing and records == old_data and lists == old_lists and old_meta.get("updated"):
        updated = old_meta["updated"]
        print(f"no data changes since {updated}")
    else:
        updated = today
        print(f"data changed; updated -> {today}")

    meta = {"updated": updated, "count": len(records)}
    js = (
        "// Generated by build.py from the Public tab of the Google Sheet. Do not edit by hand.\n"
        f"window.META = {json.dumps(meta)};\n"
        f"window.LISTS = {json.dumps(lists, ensure_ascii=False)};\n"
        f"window.DATA = {json.dumps(records, ensure_ascii=False, indent=1)};\n"
    )
    by_tier = {}
    for r in records:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
    print(f"{len(records)} public companies · " + " · ".join(f"{k}: {v}" for k, v in sorted(by_tier.items())))
    print("LISTS keys: " + ", ".join(f"{k} ({len(v)})" for k, v in lists.items()))

    if "--dry-run" in args:
        print(js[:600] + ("…" if len(js) > 600 else ""))
        return
    OUT.write_text(js, encoding="utf-8")
    print(f"wrote {OUT.relative_to(HERE)}")
    if "--no-vault" not in args:
        write_vault_mirror(records, meta["updated"], lists["tier"])

    if "--push" in args:
        subprocess.run(["git", "add", "data.js"], cwd=HERE, check=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=HERE).returncode == 1
        if not changed:
            print("no changes to publish")
            return
        # Diff against the last committed data.js (not the file we just overwrote) for the message.
        committed = subprocess.run(["git", "show", "HEAD:data.js"], cwd=HERE, capture_output=True, text=True)
        committed_data = None
        if committed.returncode == 0:
            parsed = parse_data_js(committed.stdout)
            committed_data = parsed[2] if parsed else None
        msg = commit_message(records, committed_data, meta["updated"])
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=HERE, check=True)
        print("committed:\n  " + msg.replace("\n", "\n  "))
        has_remote = subprocess.run(["git", "remote"], cwd=HERE, capture_output=True, text=True).stdout.strip()
        if has_remote:
            subprocess.run(["git", "push"], cwd=HERE, check=True)
            print("pushed; GitHub Pages will redeploy in about a minute")
        else:
            print("committed locally; no git remote configured yet, so nothing was pushed")


if __name__ == "__main__":
    main()
