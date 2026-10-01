"""Offline, reproducible extraction. Downloads must be reviewed before repinning."""
import hashlib
import json
import re
from pathlib import Path
from bs4 import BeautifulSoup
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1] / "rules/2025"
html = BeautifulSoup((ROOT / "sources/p1040-2025.html").read_text(), "html.parser")
rows = []
for tr in html.find_all("table")[1].find_all("tr"):
    cells = [td.get_text(" ", strip=True).replace(",", "") for td in tr.find_all("td", recursive=False)]
    if len(cells) == 6 and all(re.fullmatch(r"\d+", s) for s in cells):
        values = list(map(int, cells))
        rows.append({"lower": values[0], "upper": values[1], "single": values[2]})
assert rows and rows[0]["lower"] == 0 and rows[-1]["upper"] == 100000
assert all(a["upper"] == b["lower"] for a, b in zip(rows, rows[1:]))
reader = PdfReader(ROOT / "sources/i1040gi--2025.pdf")
pdf_rows = {}
for page_index in range(67, 79):
    for line in reader.pages[page_index].extract_text().splitlines():
        if re.fullmatch(r"\s*\d[\d,]*(?:\s+\d[\d,]*){5}\s*", line):
            v = [int(s.replace(",", "")) for s in line.split()]
            pdf_rows[v[0]] = {"lower": v[0], "upper": v[1], "single": v[2]}
assert all(pdf_rows.get(row["lower"]) == row for row in rows), "PDF and HTML tables disagree"
(ROOT / "single-tax-table.json").write_text(json.dumps(rows, indent=2) + "\n")
sources = [
    ("sources/i1040gi--2025.pdf", "https://www.irs.gov/pub/irs-prior/i1040gi--2025.pdf"),
    ("sources/p1040-2025.html", "https://www.irs.gov/publications/p1040"),
    ("sources/updates-2026-03-11.html", "https://www.irs.gov/forms-pubs/changes-to-the-2025-instructions-for-form-1040"),
    ("single-tax-table.json", "derived from matching archived PDF and Publication 1040"),
]
manifest = {
    "id": "US-FEDERAL-2025-single-v1", "jurisdiction": "US_FEDERAL", "tax_year": 2025,
    "source_revision": "2026-02-25", "calculation_version": "1.0.0", "table_version": "2025-single-v1",
    "standard_deduction_minor": 1575000, "rounding": "Sum original cents per line, then half-up to whole dollars; derive totals from rounded lines.",
    "reviewed_at": "2026-09-26", "review_level": "Prototype source verification; independent professional review pending",
    "update_review": "March 11 update concerns Schedule 1-A, excluded from this rule pack.",
    "verified_rows": len(rows), "sources": [{"file": f, "url": u, "sha256": hashlib.sha256((ROOT / f).read_bytes()).hexdigest()} for f,u in sources],
}
(ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(f"Pinned {len(rows)} contiguous tax rows; all match the archived PDF.")
