# /// script
# requires-python = ">=3.11"
# dependencies = ["google-auth>=2.30", "google-auth-oauthlib>=1.2", "requests>=2.32"]
# ///
"""
Pull the Public tab of the "Physical AI data stack" Google Sheet and write data.js
for the static page.

Column visibility lives in the sheet's Config tab (column | visibility | page_key).
The Public tab's formulas expose only columns marked public, so the sheet is safe
on its own. This script adds a tripwire: it also reads Config and refuses to write
if any header marked private shows up in the Public output.

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
LISTS_RANGE = "Lists!A:C"  # canonical modalities | regions | tiers, in display order
TOKEN_PATH = Path(os.environ.get("SHEETS_TOKEN_PATH", "~/.config/google-sheets-token.json")).expanduser()
HERE = Path(__file__).resolve().parent
OUT = HERE / "data.js"
VAULT_MIRROR = Path("~/Documents/obsidian_vault/!Physical AI data supply chain.md").expanduser()

LIST_FIELDS = {"mods", "region"}  # page keys whose cells hold multi-select chips (", " separated; ";" tolerated)
LIST_KEYS = {"modalities": "mods", "regions": "region", "tiers": "tier"}


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


def read_config(rows: list[list[str]]) -> tuple[dict[str, str], set[str]]:
    """Return (header -> page_key for public columns, set of private headers)."""
    public, private = {}, set()
    for row in rows[1:]:
        row = row + [""] * (3 - len(row))
        col, vis, key = (x.strip() for x in row[:3])
        if not col:
            continue
        if vis == "public":
            public[col] = key or col
        else:
            private.add(col)  # anything not explicitly public is private
    if not public:
        sys.exit("Config tab has no public columns")
    return public, private


def read_lists(rows: list[list[str]]) -> dict[str, list[str]]:
    """Lists tab -> {"mods": [...], "region": [...], "tier": [...]} in sheet order."""
    if not rows:
        sys.exit("Lists tab is empty; it should hold modalities | regions | tiers")
    header = [h.strip() for h in rows[0]]
    out: dict[str, list[str]] = {}
    for i, h in enumerate(header):
        key = LIST_KEYS.get(h)
        if key:
            out[key] = [r[i].strip() for r in rows[1:] if len(r) > i and r[i].strip()]
    for k in LIST_KEYS.values():
        if not out.get(k):
            sys.exit(f"Lists tab has no values for {k}")
    return out


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
            problems.append(f"row {i} {rec['name']!r}: tier {rec.get('tier')!r} not in Lists!tiers {lists['tier']}")
        for k in LIST_FIELDS:
            for v in rec.get(k, []):
                if v not in lists[k]:
                    problems.append(f"row {i} {rec['name']!r}: {k} value {v!r} not in the Lists tab")
        records.append({k: v for k, v in rec.items() if v not in ("", [])})
    if problems:
        sys.exit("fix these in the sheet first:\n  " + "\n  ".join(problems))
    return records


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
    import re
    text = re.sub(r"Last updated: \d{4}-\d{2}-\d{2}", f"Last updated: {updated}", text)
    VAULT_MIRROR.write_text(text, encoding="utf-8")
    print(f"mirrored {len(rows)} rows to {VAULT_MIRROR.name}")


def main() -> None:
    args = set(sys.argv[1:])
    token = creds().token
    columns, private = read_config(fetch(CONFIG_RANGE, token))
    lists = read_lists(fetch(LISTS_RANGE, token))
    records = to_records(fetch(PUBLIC_RANGE, token), columns, private, lists)
    meta = {"updated": date.today().isoformat(), "count": len(records)}
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
        subprocess.run(["git", "commit", "-q", "-m", f"Publish list: {meta['count']} companies ({meta['updated']})"], cwd=HERE, check=True)
        has_remote = subprocess.run(["git", "remote"], cwd=HERE, capture_output=True, text=True).stdout.strip()
        if has_remote:
            subprocess.run(["git", "push"], cwd=HERE, check=True)
            print("pushed; GitHub Pages will redeploy in about a minute")
        else:
            print("committed locally; no git remote configured yet, so nothing was pushed")


if __name__ == "__main__":
    main()
