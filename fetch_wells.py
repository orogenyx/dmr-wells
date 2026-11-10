#!/usr/bin/env python3
"""Fetch the ND DMR well list for each operator in operators.json and write wells/*.json.

Source: https://www.dmr.nd.gov/oilgas/findwellsvw.asp (ND Department of Mineral Resources,
Oil and Gas Division well search; public record).

Behaviour (see README.md):
  * one GET of the search form to confirm each operator exists in the dropdown,
  * one POST per operator, 2 s pause between operators, 30 s timeout, no retry loop,
  * the result table is parsed by its header names, so a column shift fails loudly,
  * on any failure the operator's previous wells/<slug>.json is kept and the failure is logged,
  * exit code 0 unless every operator failed (so the Action still commits the good files).

Standard library only (urllib + html.parser); no third-party packages.

Usage:
  python fetch_wells.py                 # all operators in operators.json
  python fetch_wells.py --operator "Phoenix Operating LLC"
  python fetch_wells.py --html saved.html --operator "Phoenix Operating LLC"   # parse a saved page, no network
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

SEARCH_URL = "https://www.dmr.nd.gov/oilgas/findwellsvw.asp"
TIMEOUT_S = 30
PAUSE_S = 2.0

ROOT = os.path.dirname(os.path.abspath(__file__))
OPERATORS_FILE = os.path.join(ROOT, "operators.json")
WELLS_DIR = os.path.join(ROOT, "wells")
INDEX_FILE = os.path.join(WELLS_DIR, "index.json")

# Columns the result table must have. Keys are our output field names, values are the DMR header texts.
REQUIRED_HEADERS = {
    "file": "File No",
    "api": "API No",
    "status": "Well Status",
    "location": "Location",
    "operator": "Operator",
    "well": "Well Name",
    "field": "Field",
}

# ND county FIPS codes as used in the middle group of a North Dakota API number (33-CCC-NNNNN).
# The DMR search does not return a county column, so the county is derived from the API number.
ND_COUNTY_BY_API_CODE = {
    "001": "ADAMS", "003": "BARNES", "005": "BENSON", "007": "BILLINGS", "009": "BOTTINEAU",
    "011": "BOWMAN", "013": "BURKE", "015": "BURLEIGH", "017": "CASS", "019": "CAVALIER",
    "021": "DICKEY", "023": "DIVIDE", "025": "DUNN", "027": "EDDY", "029": "EMMONS",
    "031": "FOSTER", "033": "GOLDEN VALLEY", "035": "GRAND FORKS", "037": "GRANT", "039": "GRIGGS",
    "041": "HETTINGER", "043": "KIDDER", "045": "LAMOURE", "047": "LOGAN", "049": "MCHENRY",
    "051": "MCINTOSH", "053": "MCKENZIE", "055": "MCLEAN", "057": "MERCER", "059": "MORTON",
    "061": "MOUNTRAIL", "063": "NELSON", "065": "OLIVER", "067": "PEMBINA", "069": "PIERCE",
    "071": "RAMSEY", "073": "RANSOM", "075": "RENVILLE", "077": "RICHLAND", "079": "ROLETTE",
    "081": "SARGENT", "083": "SHERIDAN", "085": "SIOUX", "087": "SLOPE", "089": "STARK",
    "091": "STEELE", "093": "STUTSMAN", "095": "TOWNER", "097": "TRAILL", "099": "WALSH",
    "101": "WARD", "103": "WELLS", "105": "WILLIAMS",
}


def user_agent():
    repo = os.environ.get("DMR_WELLS_REPO", "dmr-wells")
    return f"dmr-wells/1.0 (GitHub repo {repo}; scheduled daily fetch of ND DMR public well records)"


def log(msg):
    print(msg, flush=True)


def warn(msg):
    # ::warning:: makes the line show up as an annotation in the GitHub Actions summary.
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"WARNING: {msg}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ----------------------------------------------------------------------------- HTTP


def http(url, data=None):
    body = urllib.parse.urlencode(data).encode("ascii") if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": user_agent()})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def fetch_form():
    return http(SEARCH_URL)


def post_search(operator):
    # Field names and default values copied from the DMR form (hidden VTI-GROUP, blank field,
    # 0 for section/township/range, and the Submit button's own name/value).
    return http(SEARCH_URL, {
        "VTI-GROUP": "0",
        "ddmOperator": operator,
        "ddmField": " ",
        "ddmSection": "0",
        "ddmTownship": "0",
        "ddmRange": "0",
        "B1": "Submit",
    })


# ----------------------------------------------------------------------------- HTML parsing


class FormOperators(HTMLParser):
    """Collects the option values of the ddmOperator <select> on the search form."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_select = False
        self.options = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "select" and a.get("name") == "ddmOperator":
            self.in_select = True
        elif tag == "option" and self.in_select and a.get("value") is not None:
            self.options.append(a["value"].strip())

    def handle_endtag(self, tag):
        if tag == "select" and self.in_select:
            self.in_select = False


class ResultTables(HTMLParser):
    """Collects every <table> as a list of rows; each row is a list of (cell_tag, text)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._table = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._table is not None:
            if self._row is None:          # the DMR page puts <th> directly inside <thead> with no <tr>
                self._row = []
            self._cell = [tag, []]

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append((self._cell[0], " ".join("".join(self._cell[1]).split())))
            self._cell = None
        elif tag in ("tr", "thead") and self._row is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell[1].append(data)


def parse_form_operators(html):
    p = FormOperators()
    p.feed(html)
    p.close()
    return p.options


def parse_results(html):
    """Return the list of row dicts (keyed by header text) from the well results table.

    Raises ValueError if no table carries the required headers or a row's cell count differs
    from the header's, so a changed page layout fails instead of mis-filling fields.
    """
    p = ResultTables()
    p.feed(html)
    p.close()
    wanted = set(REQUIRED_HEADERS.values())
    for table in p.tables:
        if not table:
            continue
        header = [text for tag, text in table[0] if tag == "th"]
        if not wanted.issubset(header):
            continue
        rows = []
        for r in table[1:]:
            cells = [text for tag, text in r if tag == "td"]
            if not cells:
                continue
            if len(cells) != len(header):
                raise ValueError(
                    f"result row has {len(cells)} cells but header has {len(header)}: {cells[:4]}..."
                )
            rows.append(dict(zip(header, cells)))
        return rows
    raise ValueError(f"no results table with headers {sorted(wanted)} found in the page")


# ----------------------------------------------------------------------------- record shaping


def format_api(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != 10:
        return ""                      # the DMR did not give a usable API; never invent one
    return f"{digits[0:2]}-{digits[2:5]}-{digits[5:10]}"


def county_from_api(api):
    if not api:
        return ""
    return ND_COUNTY_BY_API_CODE.get(api.split("-")[1], "")


def format_location(raw):
    # DMR gives e.g. "SWSW 13-160-98" already; normalise whitespace and make sure the legal part
    # carries no spaces (e.g. "SWSW 13 - 160 - 98" -> "SWSW 13-160-98").
    text = " ".join((raw or "").split()).upper()
    m = re.fullmatch(r"([A-Z0-9/]+)\s+(\d+)\s*-\s*(\d+)\s*-\s*(\d+)", text)
    if m:
        return f"{m.group(1)} {m.group(2)}-{m.group(3)}-{m.group(4)}"
    return text


def to_record(row, operator):
    api = format_api(row[REQUIRED_HEADERS["api"]])
    return {
        "file": row[REQUIRED_HEADERS["file"]].strip(),
        "api": api,
        "well": " ".join(row[REQUIRED_HEADERS["well"]].split()),
        "operator": operator,
        "status": row[REQUIRED_HEADERS["status"]].strip(),
        "location": format_location(row[REQUIRED_HEADERS["location"]]),
        "county": county_from_api(api),
        "field": " ".join(row[REQUIRED_HEADERS["field"]].split()).upper(),
        "footages": "",
        "latitude": None,
        "longitude": None,
    }


def build_operator_doc(operator, html, fetched):
    rows = parse_results(html)
    if not rows:
        raise ValueError("the search returned zero wells; keeping the previous file")
    returned_ops = {r[REQUIRED_HEADERS["operator"]].strip().lower() for r in rows}
    if operator.lower() not in returned_ops:
        raise ValueError(f"results list operators {sorted(returned_ops)[:3]}, not {operator!r}")
    wells = [to_record(r, operator) for r in rows]
    wells.sort(key=lambda w: (w["well"].upper(), w["well"], w["file"]))
    return {"operator": operator, "fetched": fetched, "source": SEARCH_URL, "wells": wells}


# ----------------------------------------------------------------------------- files


def write_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_operators():
    doc = read_json(OPERATORS_FILE)
    ops = doc.get("operators") if isinstance(doc, dict) else None
    if not isinstance(ops, list) or not all(isinstance(o, str) and o.strip() for o in ops):
        raise SystemExit('operators.json must look like {"operators": ["Phoenix Operating LLC"]}')
    seen, out = set(), []
    for o in ops:
        o = o.strip()
        if o.lower() not in seen:
            seen.add(o.lower())
            out.append(o)
    return out


def write_index(operators, generated):
    entries = []
    for op in operators:
        slug = slugify(op)
        path = os.path.join(WELLS_DIR, f"{slug}.json")
        if not os.path.exists(path):
            warn(f"{op}: no wells file yet, left out of index.json")
            continue
        doc = read_json(path)
        entries.append({
            "operator": op,
            "slug": slug,
            "file": f"wells/{slug}.json",
            "wells": len(doc.get("wells", [])),
            "fetched": doc.get("fetched", ""),
        })
    write_json(INDEX_FILE, {"generated": generated, "operators": entries})
    return entries


# ----------------------------------------------------------------------------- main


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--operator", action="append", help="only this operator (repeatable)")
    ap.add_argument("--html", help="parse this saved result page instead of fetching (needs one --operator)")
    args = ap.parse_args(argv)

    operators = load_operators()
    if args.operator:
        operators = [o.strip() for o in args.operator]
    if args.html and len(operators) != 1:
        ap.error("--html needs exactly one --operator")

    os.makedirs(WELLS_DIR, exist_ok=True)
    log(f"fetch_wells: {len(operators)} operator(s), {now_iso()}")

    known = None
    if not args.html:
        try:
            known = parse_form_operators(fetch_form())
            if not known:
                raise ValueError("operator dropdown not found on the form page")
            log(f"form loaded: {len(known)} operators in the DMR dropdown")
        except Exception as e:         # noqa: BLE001 - any failure here just skips the dropdown check
            warn(f"could not load the search form ({e}); skipping the operator-name check")
            known = None

    ok, failed = [], []
    for i, op in enumerate(operators):
        if i and not args.html:
            time.sleep(PAUSE_S)
        slug = slugify(op)
        path = os.path.join(WELLS_DIR, f"{slug}.json")
        try:
            if known is not None and op not in known:
                near = [k for k in known if k.lower() == op.lower()] or \
                       [k for k in known if slugify(op).split("-")[0] in k.lower()][:5]
                raise ValueError(f"not in the DMR operator dropdown (spelling must match exactly); "
                                 f"similar: {near or 'none'}")
            if args.html:
                with open(args.html, encoding="utf-8", errors="replace") as f:
                    html = f.read()
            else:
                html = post_search(op)
            fetched = now_iso()
            doc = build_operator_doc(op, html, fetched)
            write_json(path, doc)
            log(f"{op}: {len(doc['wells'])} wells -> wells/{slug}.json")
            ok.append(op)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as e:
            kept = "previous file kept" if os.path.exists(path) else "no previous file"
            warn(f"{op}: FAILED ({e.__class__.__name__}: {e}); {kept}")
            failed.append(op)

    entries = write_index(operators, now_iso())
    log(f"index.json: {len(entries)} operator(s); ok={len(ok)} failed={len(failed)}")

    if operators and not ok:
        log("every operator failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
