"""Deterministic document extraction over OCR-style text lines.

Every extracted fact carries a source locator (`doc:<id>#p<page>:l<line>`), a confidence and uncertainty flags.
Document text is untrusted data: it is parsed for facts only and never interpreted as instructions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .money import parse_amount_to_minor

EXTRACTION_VERSION = "extract-v1"

TZ_OFFSETS = {"UTC": 0, "Z": 0, "GMT": 0, "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7}

CATEGORY_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("electronics", ["headphone", "earbud", "charger", "cable", "adapter", "power bank", "phone", "tablet", "laptop", "camera"]),
    ("alcohol", ["wine", "beer", "whiskey", "vodka", "liquor", "champagne"]),
    ("jewelry", ["necklace", "bracelet", "earring", "wristwatch"]),
    ("luxury", ["designer", "perfume", "cologne", "handbag", "silk"]),
    ("toiletries", ["toothbrush", "toothpaste", "deodorant", "razor", "shave", "shampoo", "conditioner", "soap", "comb", "hairbrush", "contact lens", "sunscreen", "lotion", "floss", "tampon"]),
    ("clothing", ["shirt", "t-shirt", "sock", "jacket", "fleece", "underwear", "pants", "jeans", "sweater", "hoodie", "shoe", "sandals", "legging", "shorts", "dress", "pyjama", "pajama", "gloves", "scarf"]),
    ("essentials", ["medication", "medicine", "prescription", "glasses", "baby formula", "diaper", "nappy"]),
]

INSTRUCTION_PATTERNS = [
    r"ignore (all )?(previous|prior) instructions",
    r"note to (the )?ai",
    r"\bassistant\b.*\b(update|change|approve)\b",
    r"\b(update|change)\b.*\b(bank|payout|account|routing)\b",
    r"approve the claim",
]

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}):(\d{2}))?(?:\s*([A-Z]{1,4}))?")
ITEM_RE = re.compile(r"^(\d+)\s*x\s+(.+?)\s{2,}\$?(\d+\.\d{2})\s*$")
TOTAL_RE = re.compile(r"^TOTAL\s+\$?(\d+\.\d{2})\s*$", re.IGNORECASE)
STATEMENT_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s{2,}(.+?)\s{2,}(\d+\.\d{2})\s*$")


@dataclass
class ExtractedFact:
    fact_type: str
    value: Dict[str, Any]
    document_id: str
    source_locator: str
    confidence: str = "high"  # high | low
    uncertainty_flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fact_type": self.fact_type,
            "value": self.value,
            "evidence_id": self.document_id,
            "source_locator": self.source_locator,
            "confidence": self.confidence,
            "uncertainty_flags": list(self.uncertainty_flags),
        }


@dataclass
class ExtractedItem:
    description: str
    quantity: int
    amount_minor: int
    category: str  # known category or "unknown"
    source_locator: str


@dataclass
class ExtractedReceipt:
    document_id: str
    merchant: str
    purchased_at: Optional[str]
    total_minor: Optional[int]
    currency: str
    items: List[ExtractedItem]
    source_locator: str
    uncertainty_flags: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def items_total_minor(self) -> int:
        return sum(i.amount_minor for i in self.items)


@dataclass
class ExtractionResult:
    document_id: str
    doc_type: str
    facts: List[ExtractedFact]
    receipt: Optional[ExtractedReceipt]
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "document_id": self.document_id,
            "doc_type": self.doc_type,
            "extraction_version": EXTRACTION_VERSION,
            "facts": [f.to_dict() for f in self.facts],
            "warnings": list(self.warnings),
        }
        if self.receipt:
            r = self.receipt
            out["receipt"] = {
                "merchant": r.merchant,
                "purchased_at": r.purchased_at,
                "total_minor": r.total_minor,
                "currency": r.currency,
                "items": [
                    {"description": i.description, "quantity": i.quantity, "amount_minor": i.amount_minor, "category": i.category, "source_locator": i.source_locator}
                    for i in r.items
                ],
                "uncertainty_flags": list(r.uncertainty_flags),
            }
        return out


def locator(doc_id: str, page: int, line: int) -> str:
    return f"doc:{doc_id}#p{page}:l{line}"


def parse_datetime(text: str) -> Tuple[Optional[str], List[str]]:
    """Returns (ISO UTC string or None, flags)."""
    m = DATE_RE.search(text)
    if not m:
        return None, ["date_missing"]
    flags: List[str] = []
    date_part, hh, mm, tz = m.group(1), m.group(2), m.group(3), m.group(4)
    if hh is None:
        flags.append("time_missing")
        hh, mm = "00", "00"
    offset = 0
    if tz and tz in TZ_OFFSETS:
        offset = TZ_OFFSETS[tz]
    else:
        flags.append("timezone_assumed_utc")
    try:
        naive = datetime.fromisoformat(f"{date_part}T{hh}:{mm}:00")
    except ValueError:
        return None, ["date_unparseable"]
    utc = (naive - timedelta(hours=offset)).replace(tzinfo=timezone.utc)
    return utc.isoformat().replace("+00:00", "Z"), flags


def classify_item(description: str) -> str:
    lower = description.lower()
    for category, keywords in CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in lower:
                return category
    return "unknown"


def detect_instruction_text(lines: List[str]) -> List[str]:
    hits = []
    for idx, line in enumerate(lines, start=1):
        low = line.lower()
        for pat in INSTRUCTION_PATTERNS:
            if re.search(pat, low):
                hits.append(f"l{idx}")
                break
    return hits


def _find_line(lines: List[str], pattern: str) -> Optional[Tuple[int, re.Match]]:
    rx = re.compile(pattern, re.IGNORECASE)
    for idx, line in enumerate(lines, start=1):
        m = rx.search(line)
        if m:
            return idx, m
    return None


def extract_document(doc_id: str, doc_type: str, pages: List[List[str]]) -> ExtractionResult:
    lines = pages[0] if pages else []
    page = 1
    facts: List[ExtractedFact] = []
    warnings: List[str] = []
    receipt: Optional[ExtractedReceipt] = None

    instruction_hits = detect_instruction_text(lines)
    if instruction_hits:
        warnings.append(f"instruction_like_text_ignored:{','.join(instruction_hits)}")

    if doc_type == "itinerary":
        p = _find_line(lines, r"Passenger:\s*(.+)$")
        if p:
            facts.append(ExtractedFact("passenger_name", {"name": p[1].group(1).strip()}, doc_id, locator(doc_id, page, p[0])))
        for idx, line in enumerate(lines, start=1):
            m = re.search(r"Flight\s+([A-Z]{2}\d{2,4})\s+(.+?)\s*\((\w{3})\)\s*->\s*(.+?)\s*\((\w{3})\)", line)
            if m:
                facts.append(ExtractedFact("flight_segment", {"flight": m.group(1), "origin": m.group(3), "origin_city": m.group(2).strip(), "destination": m.group(5), "destination_city": m.group(4).strip()}, doc_id, locator(doc_id, page, idx)))
                arr = _find_line(lines[idx:], r"Arrives:\s*(.+)$")
                if arr and arr[0] <= 2:
                    ts, flags = parse_datetime(arr[1].group(1))
                    facts.append(ExtractedFact("scheduled_arrival_at", {"flight": m.group(1), "at": ts, "destination": m.group(5)}, doc_id, locator(doc_id, page, idx + arr[0]), "low" if flags or ts is None else "high", flags))
        home = _find_line(lines, r"Home address on file:\s*(.+)$")
        if home:
            facts.append(ExtractedFact("home_location", {"city": home[1].group(1).strip()}, doc_id, locator(doc_id, page, home[0])))

    elif doc_type == "baggage_delay_report":
        pir = _find_line(lines, r"PIR reference:\s*(\S+)")
        if pir:
            facts.append(ExtractedFact("pir_reference", {"reference": pir[1].group(1)}, doc_id, locator(doc_id, page, pir[0])))
        p = _find_line(lines, r"Passenger:\s*(.+)$")
        if p:
            facts.append(ExtractedFact("passenger_name", {"name": p[1].group(1).strip()}, doc_id, locator(doc_id, page, p[0])))
        fl = _find_line(lines, r"Flight:\s*([A-Z]{2}\d{2,4})\s+Date:\s*(\d{4}-\d{2}-\d{2})")
        if fl:
            facts.append(ExtractedFact("delayed_flight", {"flight": fl[1].group(1), "date": fl[1].group(2)}, doc_id, locator(doc_id, page, fl[0])))
        tag = _find_line(lines, r"Bag tag:\s*(\S+)")
        if tag:
            facts.append(ExtractedFact("bag_tag", {"tag": tag[1].group(1)}, doc_id, locator(doc_id, page, tag[0])))
        rep = _find_line(lines, r"Reported at:\s*(.+)$")
        if rep:
            ts, flags = parse_datetime(rep[1].group(1))
            facts.append(ExtractedFact("delay_reported_at", {"at": ts}, doc_id, locator(doc_id, page, rep[0]), "low" if flags or ts is None else "high", flags))
        else:
            warnings.append("delay_reported_at_missing")
        status = _find_line(lines, r"Status:\s*(.+)$")
        if status:
            facts.append(ExtractedFact("baggage_status", {"status": status[1].group(1).strip()}, doc_id, locator(doc_id, page, status[0])))
        deliv = _find_line(lines, r"Delivery address:\s*(.+)$")
        if deliv:
            addr = deliv[1].group(1).strip()
            facts.append(ExtractedFact("delivery_address", {"address": addr, "is_home": "home" in addr.lower()}, doc_id, locator(doc_id, page, deliv[0])))

    elif doc_type == "baggage_arrival_confirmation":
        pir = _find_line(lines, r"PIR reference:\s*(\S+)")
        if pir:
            facts.append(ExtractedFact("pir_reference", {"reference": pir[1].group(1)}, doc_id, locator(doc_id, page, pir[0])))
        tag = _find_line(lines, r"Bag tag:\s*(\S+)")
        if tag:
            facts.append(ExtractedFact("bag_tag", {"tag": tag[1].group(1)}, doc_id, locator(doc_id, page, tag[0])))
        dl = _find_line(lines, r"Delivered at:\s*(.+)$")
        if dl:
            ts, flags = parse_datetime(dl[1].group(1))
            facts.append(ExtractedFact("baggage_delivered_at", {"at": ts}, doc_id, locator(doc_id, page, dl[0]), "low" if flags or ts is None else "high", flags))
        else:
            warnings.append("baggage_delivered_at_missing")

    elif doc_type == "receipt":
        receipt = _extract_receipt(doc_id, lines, page)
        facts.append(
            ExtractedFact(
                "receipt",
                {"merchant": receipt.merchant, "purchased_at": receipt.purchased_at, "total_minor": receipt.total_minor, "currency": receipt.currency, "item_count": len(receipt.items)},
                doc_id,
                receipt.source_locator,
                "low" if receipt.uncertainty_flags else "high",
                receipt.uncertainty_flags,
            )
        )
        warnings.extend(receipt.warnings)

    elif doc_type == "card_statement":
        for idx, line in enumerate(lines, start=1):
            m = STATEMENT_LINE_RE.match(line)
            if m:
                amount = parse_amount_to_minor(m.group(3))
                facts.append(ExtractedFact("card_statement_line", {"date": m.group(1), "merchant": m.group(2).strip(), "amount_minor": amount, "currency": "USD"}, doc_id, locator(doc_id, page, idx), "low", ["date_only_no_time"]))
    else:
        warnings.append(f"unsupported_doc_type:{doc_type}")

    return ExtractionResult(doc_id, doc_type, facts, receipt, warnings)


def _extract_receipt(doc_id: str, lines: List[str], page: int) -> ExtractedReceipt:
    merchant = lines[0].strip() if lines else "UNKNOWN MERCHANT"
    flags: List[str] = []
    warnings: List[str] = []
    purchased_at: Optional[str] = None
    date_line = _find_line(lines, r"^Date:\s*(.+)$")
    if date_line:
        purchased_at, dflags = parse_datetime(date_line[1].group(1))
        flags.extend(dflags)
    else:
        flags.append("purchase_date_missing")
    items: List[ExtractedItem] = []
    total_minor: Optional[int] = None
    for idx, line in enumerate(lines, start=1):
        im = ITEM_RE.match(line.strip()) or ITEM_RE.match(line)
        if im:
            amount = parse_amount_to_minor(im.group(3))
            if amount is None:
                continue
            desc = im.group(2).strip()
            items.append(ExtractedItem(desc, int(im.group(1)), amount, classify_item(desc), locator(doc_id, page, idx)))
            continue
        tm = TOTAL_RE.match(line.strip())
        if tm:
            total_minor = parse_amount_to_minor(tm.group(1))
    if total_minor is None:
        flags.append("total_missing")
    elif items and sum(i.amount_minor for i in items) != total_minor:
        flags.append("items_total_mismatch")
    if not items:
        flags.append("no_itemized_lines")
    if any(i.category == "unknown" for i in items):
        flags.append("item_category_unknown")
    return ExtractedReceipt(doc_id, merchant, purchased_at, total_minor, "USD", items, locator(doc_id, page, 1), flags, warnings)
