"""Tiny stdlib .xlsx reader (zipfile + ElementTree) — enough for the simple sheets the
public agencies publish (HCA termination list, NPPES deactivation report, ...).

Returns rows as lists of cell strings in column order. Shared strings and inline strings
are resolved; numeric cells come back as their raw text (Excel date serials included —
use excel_date() to convert those).
"""
import datetime
import io
import re
import xml.etree.ElementTree as ET
import zipfile

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_index(ref):
    """'C7' -> 2 (zero-based column)."""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def xlsx_rows(data, sheet_index=0):
    """All rows of one worksheet from xlsx bytes, as lists of strings (gaps filled)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        ss = ["".join(t.text or "" for t in si.iter(f"{_NS}t"))
              for si in root.findall(f"{_NS}si")]
    sheets = sorted(n for n in z.namelist()
                    if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    if not sheets:
        return []
    out = []
    root = ET.fromstring(z.read(sheets[sheet_index]))
    for row in root.find(f"{_NS}sheetData").findall(f"{_NS}row"):
        cells = {}
        for c in row.findall(f"{_NS}c"):
            idx = _col_index(c.get("r", "A"))
            if c.get("t") == "inlineStr":
                cells[idx] = "".join(t.text or "" for t in c.iter(f"{_NS}t"))
                continue
            v = c.find(f"{_NS}v")
            if v is None or v.text is None:
                continue
            cells[idx] = ss[int(v.text)] if c.get("t") == "s" else v.text
        width = max(cells) + 1 if cells else 0
        out.append([cells.get(i, "") for i in range(width)])
    return out


def excel_date(serial):
    """Excel date serial ('45190' / 45190.0) -> 'YYYY-MM-DD', or '' if not a serial."""
    try:
        n = float(str(serial).strip())
    except (TypeError, ValueError):
        return ""
    if not 20000 <= n <= 80000:           # plausible 1954..2118 window
        return ""
    return (datetime.date(1899, 12, 30)
            + datetime.timedelta(days=int(n))).isoformat()
