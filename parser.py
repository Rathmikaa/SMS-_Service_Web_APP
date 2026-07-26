"""
parser.py
Extracts property-tax-due records from GCC (Greater Chennai Corporation)
style PDF reports. Handles both known table layouts:

  Layout A: Sl.No | Dn | Official Name | Bill Number | Old Bill Number |
            Owner Name | New Door No | Old Door No | Street | Mobile No. |
            Property Type | Property Usage | Current Tax | Current Tax Due |
            Arrear Due + | Balance Amount | H Y PEN | Remarks

  Layout B: Sl.No | Dn | Bill Number | Old Bill Number | Owner Name |
            New Door No | Old Door No | Street | Mobile No. |
            Property Type | Property Usage | Current Tax | Current Tax Due |
            Arrear tax due + | Balance Amount | Remarks

The parser is header-driven (not position-driven): it reads the header row
on every page, maps each column to a canonical field name using fuzzy
keyword matching, and normalizes rows into a consistent schema. This means
it will keep working even if a future report adds/drops/reorders columns,
as long as the header wording stays roughly similar.
"""

import re
import pdfplumber

# Canonical field -> list of keyword fragments that identify the source column.
# Matching is done against the header cell text, lowercased, newlines removed.
COLUMN_KEYWORDS = {
    "sl_no": ["sl.no", "sl no", "slno"],
    "dn": ["dn"],
    "bill_number": ["bill number", "bill numbe"],
    "old_bill_number": ["old bill"],
    "owner_name": ["owner name"],
    "new_door_no": ["new door"],
    "old_door_no": ["old door"],
    "street": ["street"],
    "mobile": ["mobile"],
    "property_type": ["property type", "propert", "type"],
    "property_usage": ["property usage", "usage"],
    "current_tax": ["current tax", "current\ntax"],  # matched before current_tax_due
    "current_tax_due": ["current tax due", "current tax\ndue"],
    "arrear_due": ["arrear"],
    "balance_amount": ["balance"],
    "remarks": ["remarks", "h y pen"],
}

# Order matters: more specific keys first so "current tax due" isn't
# accidentally matched by the shorter "current tax" pattern.
FIELD_MATCH_ORDER = [
    "sl_no", "dn", "bill_number", "old_bill_number", "owner_name",
    "new_door_no", "old_door_no", "street", "mobile",
    "property_type", "property_usage", "current_tax_due", "current_tax",
    "arrear_due", "balance_amount", "remarks",
]

NUMERIC_FIELDS = {"current_tax", "current_tax_due", "arrear_due", "balance_amount"}

HEADER_HINTS = {"sl.no", "sl no", "slno"}


def _fix_char_doubling(text):
    """Some rows in these GCC report PDFs (typically ones that straddle a
    page/table boundary) have their text rendered twice, one glyph run
    stacked on the other, which pdfplumber reconstructs as every character
    literally doubled: 'A RAJI' -> 'AA RRAAJJII', '46654' -> '4466665544'.

    Detect this per whitespace-separated token: if the token has even length
    and every adjacent pair of characters is identical (tok[0]==tok[1],
    tok[2]==tok[3], ...), collapse it to every other character. This exact
    pattern essentially never occurs in genuine text/numbers, so it's a safe,
    narrowly-targeted fix rather than a blunt de-duplication."""
    if not text:
        return text
    parts = re.split(r"(\s+)", text)
    out = []
    for tok in parts:
        if tok.strip() and len(tok) >= 2 and len(tok) % 2 == 0:
            if all(tok[i] == tok[i + 1] for i in range(0, len(tok), 2)):
                tok = tok[0::2]
        out.append(tok)
    return "".join(out)


def _clean_cell(text):
    if text is None:
        return ""
    text = str(text).replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = _fix_char_doubling(text)
    return text


def _norm_header(text):
    return _clean_cell(text).lower()


def _looks_like_header_row(row):
    if not row:
        return False
    first = _norm_header(row[0])
    return first in HEADER_HINTS


def _map_headers(header_row):
    """Return dict: column_index -> canonical_field_name"""
    mapping = {}
    used_fields = set()
    normalized = [_norm_header(c) for c in header_row]
    for idx, cell in enumerate(normalized):
        if not cell:
            continue
        for field in FIELD_MATCH_ORDER:
            if field in used_fields:
                continue
            keywords = COLUMN_KEYWORDS[field]
            if any(kw in cell for kw in keywords):
                mapping[idx] = field
                used_fields.add(field)
                break
    return mapping


def _clean_numeric(value):
    if value is None:
        return 0
    v = _clean_cell(value)
    v = v.replace(",", "").replace("Rs", "").replace("₹", "").strip()
    if v in ("", "N/A", "null", "NA", "-"):
        return 0
    try:
        return int(float(v))
    except ValueError:
        return 0


def _clean_mobile(value):
    v = _clean_cell(value)
    digits = re.sub(r"\D", "", v)
    if len(digits) == 10:
        return digits
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    return ""  # not a usable 10-digit Indian mobile number


def _clean_door(value):
    v = _clean_cell(value)
    # Strip labels like "New No :12" -> "12"
    v = re.sub(r"(?i)^(new|old)\s*no\s*:?", "", v).strip()
    if v.lower() in ("null", "n/a", ""):
        return ""
    return v


def _clean_code(value):
    """Bill numbers / old bill numbers never legitimately contain spaces;
    PDF text extraction sometimes inserts one where the source PDF wrapped
    the code across lines (e.g. '13-174- 03118- 000'). Strip all spaces."""
    v = _clean_cell(value).replace(" ", "")
    if v.upper() in ("N/A", "NULL", ""):
        return "N/A"
    return v


KNOWN_PROPERTY_TYPES = {
    "independentbuilding": "Independent Building",
    "flatsinmultistoriedbuilding": "Flats in Multi Storied Building",
    "flat": "Flat",
    "superstructure": "Super Structure",
    "educationalinstitution": "Educational Institution",
    "vacantland": "Vacant Land",
}

KNOWN_PROPERTY_USAGE = {
    "residential": "Residential",
    "nonresidential": "Non-Residential",
    "mixeduse": "Mixed-Use",
    "na": "N/A",
}


def _normalize_enum(value, known_map):
    """PDF cell-wrapping sometimes splits a single word across lines,
    producing extraction artifacts like 'Independe nt' or 'Residentia l'.
    Collapse all whitespace/hyphens and match against a whitelist of known
    values; fall back to the cleaned original text if no match is found."""
    v = _clean_cell(value)
    key = re.sub(r"[\s\-]", "", v).lower()
    return known_map.get(key, v)


def parse_pdf(path, source_filename=None):
    """
    Parses a GCC property tax PDF and returns a list of dicts (canonical schema).
    """
    records = []
    source_filename = source_filename or path

    with pdfplumber.open(path) as pdf:
        current_mapping = None
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                for row in table:
                    if not row or all((c is None or _clean_cell(c) == "") for c in row):
                        continue
                    if _looks_like_header_row(row):
                        current_mapping = _map_headers(row)
                        continue
                    if current_mapping is None:
                        # No header seen yet on this page/table -- skip stray rows
                        # (e.g. title banners like "Zone-13 Property tax ... cases on ...")
                        continue

                    rec = {field: "" for field in FIELD_MATCH_ORDER}
                    for idx, field in current_mapping.items():
                        if idx < len(row):
                            rec[field] = _clean_cell(row[idx])

                    # Skip rows that don't actually have a sl_no (garbage / repeated titles)
                    if not rec["sl_no"] or not rec["sl_no"].isdigit():
                        continue

                    rec["new_door_no"] = _clean_door(rec["new_door_no"])
                    rec["old_door_no"] = _clean_door(rec["old_door_no"])
                    rec["mobile"] = _clean_mobile(rec["mobile"])
                    rec["bill_number"] = _clean_code(rec["bill_number"])
                    rec["old_bill_number"] = _clean_code(rec["old_bill_number"])
                    rec["property_type"] = _normalize_enum(rec["property_type"], KNOWN_PROPERTY_TYPES)
                    rec["property_usage"] = _normalize_enum(rec["property_usage"], KNOWN_PROPERTY_USAGE)
                    for f in NUMERIC_FIELDS:
                        rec[f] = _clean_numeric(rec[f])
                    rec["dn"] = rec["dn"] or "172"
                    rec["source_file"] = source_filename

                    # Flag rows whose bill number doesn't look complete (e.g. missing
                    # trailing digit group). This happens on a small number of rows in
                    # these reports where the source PDF itself renders the row text
                    # twice/overlapping across a page or table boundary -- our doubling
                    # fix above recovers the row correctly in almost all cases, but a
                    # few fields (bill number tail, property type/usage wording) can
                    # still come out truncated. Flag so a human can double check before
                    # an SMS goes out with a wrong bill number.
                    rec["needs_review"] = bool(
                        rec["bill_number"] != "N/A"
                        and not re.match(r"^\d{2}-\d{3}-\d{4,6}-\d{3}$", rec["bill_number"])
                    )

                    records.append(rec)

    return records


if __name__ == "__main__":
    import sys
    import json
    for p in sys.argv[1:]:
        recs = parse_pdf(p)
        print(f"{p}: {len(recs)} records")
        print(json.dumps(recs[:2], indent=2))
