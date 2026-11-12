"""
The four steps: Parse -> Detect -> Verify -> Sign.

Every function takes and returns plain Python values (dicts, lists, strings, numbers), so each
result can be stored as JSON in the job record and shown in the GUI. Nothing here knows about the
web, the database or the job queue.

Coordinates are PDF points (72 pt = 1 inch) measured from the top-left corner of the page, the way
PyMuPDF reports them. A rectangle is a list [x0, y0, x1, y1].

The sections, in order:
    Helpers used by every step
    Step 1, Parse     read_page(): words, lines, form fields, drawings, the client's signatures
    Step 2, Detect    analyze(): spots, blocks, dates, parties, owners, placements, red flags
    Step 3, Verify    run_checks(): the 8-point auto-sign check
    Step 4, Sign      sign_pdf(): stamp a copy, then check the copy
    Pictures          page previews, and cleaning the signature image
"""

import hashlib
import io
import json
import os
import re
import shutil
import time
from itertools import combinations
from pathlib import Path

import pymupdf
from PIL import Image
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext
from rapidfuzz import fuzz

from autosign.policy import (
    BLOCK_REACH, DATE_FONT_SIZE, DOC_TYPE_FAMILY, DOC_TYPE_PATTERNS, DROP_RATIOS, ENTITY_WORDS, EVIDENCE_TEXT,
    EXECUTION_PATTERNS, FIELD_WORDS, HEADER_REACH, INSTRUCTION_PATTERNS, KNOWN_ROLES, LEGAL_SUFFIXES,
    MAX_LABEL_WORDS, MIN_LINE_WIDTH, NAMELESS_EVIDENCE, NEVER_WORDS, NOT_A_SPOT_WORDS, NOT_SIGNING_DATE_WORDS,
    ROW_TOLERANCE, RULES_VERSION, SEGMENT_GAP, SIGNATURE_HEIGHTS, SIGN_WORDS, STRONG_EVIDENCE,
)

# =============================================================================
# Helpers used by every step
# =============================================================================

def norm(text):
    """Lowercase, straighten quotes, drop blank runs (____, .....), squeeze spaces."""
    text = (text or "").replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u00a0", " ")
    text = re.sub(r"_+|\u2026+|\.{3,}", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def tokens(text):
    """The words of a text, lowercase, without punctuation."""
    return re.findall(r"[a-z0-9]+", norm(text))


def phrase_pattern(phrase):
    """Regex that finds a phrase whatever the case, punctuation or spacing ('ACME ANALYTICS, INC.')."""
    words = re.findall(r"[A-Za-z0-9]+", phrase or "")
    if not words:
        return None
    return r"(?<![A-Za-z0-9])" + r"[\W_]*".join(re.escape(w) for w in words) + r"(?![A-Za-z0-9])"


def find_phrase(text, phrase):
    """Return the first match of the phrase in the text, or None."""
    pattern = phrase_pattern(phrase)
    if pattern is None:
        return None
    return re.search(pattern, text or "", flags=re.IGNORECASE)


def match_score(needle, haystack):
    """How well `needle` appears in `haystack`, from 0 to 100 (fuzzy, so small slips still match)."""
    if needle and len(needle) <= len(haystack):
        return fuzz.partial_ratio(needle, haystack)      # the best match anywhere inside the haystack
    return fuzz.ratio(needle, haystack)


def company_names(settings):
    """Your company's name and the other names it goes by."""
    return [settings["company_name"]] + settings["company_aliases"]


def rect_of(item):
    return [item["x0"], item["y0"], item["x1"], item["y1"]]


def rounded(rect):
    return [round(v, 1) for v in rect]


def bounds(items):
    """The smallest box around some words, as {x0, y0, x1, y1}."""
    return {"x0": min(i["x0"] for i in items), "y0": min(i["y0"] for i in items),
            "x1": max(i["x1"] for i in items), "y1": max(i["y1"] for i in items)}


def center_x(item):
    return (item["x0"] + item["x1"]) / 2


def intersects(a, b, pad=0.6):
    """True when two rectangles overlap by more than `pad` points (touching is allowed)."""
    return a[0] < b[2] - pad and b[0] < a[2] - pad and a[1] < b[3] - pad and b[1] < a[3] - pad


def overlap(a0, a1, b0, b1):
    """How long the ranges a0-a1 and b0-b1 overlap (0 when they don't)."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def overlap_area(a, b):
    return overlap(a[0], a[2], b[0], b[2]) * overlap(a[1], a[3], b[1], b[3])


def file_fingerprint(path):
    """SHA-256 of the file's bytes: the same content always gives the same fingerprint."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def bytes_fingerprint(data):
    return hashlib.sha256(data).hexdigest()


# =============================================================================
# Step 1, Parse: the client's digital signatures
# =============================================================================

def read_signatures(pdf_path):
    """Digital signatures already in the file, with the certification lock level (DocMDP)."""
    found = []
    with open(pdf_path, "rb") as f:
        reader = PdfFileReader(f, strict=False)
        for sig in reader.embedded_signatures:
            level = sig.docmdp_level
            try:
                signer = sig.signer_cert.subject.human_friendly
            except Exception:
                signer = "unknown signer"
            found.append({
                "field": sig.field_name,
                "certification": level is not None,
                "lock_level": int(level.value) if level is not None else None,
                "signer": signer,
            })
    return found


def has_signature_fields(doc):
    """True when the PDF has signature form fields, or says it has (/SigFlags)."""
    return doc.get_sigflags() > 0 or any(
        widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE for page in doc for widget in page.widgets())


def signature_state(pdf_path, doc=None):
    """The client's digital signatures, the lock level they set, and how our signature may be added.

    Always read from the file (never only when /SigFlags is set: some PDFs leave it out), before
    anything else, and again right before signing. 'blocked' says why signing must not happen.
    'mode' is 'annotation' for PDFs that are (or may be) signed: they get our stamp as an annotation
    and an append-only save, so their signed bytes stay untouched. Other PDFs get 'content'.
    """
    try:
        signatures, error = read_signatures(pdf_path), None
    except Exception as exc:
        # pyHanko couldn't parse the file. That only matters when there is a signature to protect.
        signatures = []
        if doc is not None:
            fields_found = has_signature_fields(doc)
        else:
            with pymupdf.open(pdf_path) as opened:
                fields_found = has_signature_fields(opened)
        error = exc if fields_found else None

    locks = [s["lock_level"] for s in signatures if s["lock_level"]]
    lock = min(locks, default=None)
    if error is not None:
        blocked = f"The PDF has digital signature fields that can't be read ({error}), so signing could break them"
    elif lock == 1:
        blocked = "The client certified this PDF with lock level 1: no changes are allowed"
    elif lock == 2:
        blocked = ("The client certified this PDF with lock level 2 (form filling and signing only); "
                   "a stamped image would break their certification")
    else:
        blocked = None
    mode = "annotation" if signatures or error is not None else "content"
    return {"signatures": signatures, "lock_level": lock, "blocked": blocked, "mode": mode}


def validate_signatures(pdf_path):
    """Check every digital signature: are the signed bytes untouched and the maths valid?

    Whether the signer's certificate is trusted is a separate question this check doesn't ask,
    so no trust list is loaded (an empty one is passed on purpose).
    """
    results = []
    with open(pdf_path, "rb") as f:
        reader = PdfFileReader(f, strict=False)
        for sig in reader.embedded_signatures:
            status = validate_pdf_signature(sig, signer_validation_context=ValidationContext(trust_roots=[]),
                                            skip_diff=True)
            results.append({"field": sig.field_name, "intact": bool(status.intact), "valid": bool(status.valid)})
    return results


# =============================================================================
# Step 1, Parse: words, lines, form fields and drawings of one page
# =============================================================================

def read_words(page):
    """Words with boxes. Runs of ____ or ..... are kept apart as 'blanks' (they are lines, not text)."""
    words, blanks = [], []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        clean = re.sub(r"_{3,}|\.{3,}|\u2026+", "", text)
        if not clean.strip(" ,.;:()$/-"):
            blanks.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "dots": "_" not in text})
            continue
        if len(clean) < len(text):
            # "By:______" is one word: keep the box of the text part only (estimated by length).
            share = len(clean) / len(text)
            if text.startswith(clean):
                x1 = x0 + (x1 - x0) * share
            elif text.endswith(clean):
                x0 = x1 - (x1 - x0) * share
        words.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": clean})
    return words, blanks


def read_fields(page):
    """Form fields with positions. Party names and dates often live here, not in the page text."""
    kinds = {pymupdf.PDF_WIDGET_TYPE_SIGNATURE: "signature", pymupdf.PDF_WIDGET_TYPE_TEXT: "text"}
    fields = []
    for widget in page.widgets():
        value = widget.field_value if isinstance(widget.field_value, str) else ""
        fields.append({
            "name": widget.field_name or "",
            "kind": kinds.get(widget.field_type, "other"),
            "value": value.strip(),
            "rect": list(widget.rect),
            "xref": widget.xref,
        })
    return fields


def tag_field_words(words, fields):
    """Mark the words that are form-field values, so they are never read as labels.

    PyMuPDF already returns the visible text of filled fields (and of a client's visible digital
    signature, "Digitally signed by..."). A text field's value is added here only when it shows no text.
    """
    for f in fields:
        if f["kind"] not in ("text", "signature"):
            continue
        r = f["rect"]
        inside = [w for w in words if r[0] <= center_x(w) <= r[2] and r[1] <= (w["y0"] + w["y1"]) / 2 <= r[3]]
        for w in inside:
            w["field"] = f["xref"]
        if f["kind"] == "text" and f["value"] and not inside:
            words.append({"x0": r[0], "y0": r[1], "x1": r[2], "y1": r[3], "text": f["value"], "field": f["xref"]})


def make_line(x0, x1, y, source):
    return {"x0": x0, "x1": x1, "y": y, "source": source}


def read_drawings(page):
    """Horizontal rules, vertical rules, outlined boxes, curves (possible ink) and dark fills."""
    found = {"lines": [], "verticals": [], "boxes": [], "curves": [], "dark_fills": []}
    for path in page.get_drawings():
        fill = path.get("fill")
        if fill and 0.3 * fill[0] + 0.59 * fill[1] + 0.11 * fill[2] < 0.5:       # darker than mid-grey
            found["dark_fills"].append(list(path["rect"]))
        for item in path["items"]:
            kind = item[0]
            if kind == "l":                                                     # a straight line
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 1 and abs(p1.x - p2.x) >= MIN_LINE_WIDTH:
                    found["lines"].append(make_line(min(p1.x, p2.x), max(p1.x, p2.x), (p1.y + p2.y) / 2, "drawn"))
                elif abs(p1.x - p2.x) < 1 and abs(p1.y - p2.y) >= 6:
                    found["verticals"].append([p1.x - 0.5, min(p1.y, p2.y), p1.x + 0.5, max(p1.y, p2.y)])
            elif kind == "re":                                                  # a rectangle
                r = item[1]
                if r.height <= 2.5 and r.width >= MIN_LINE_WIDTH:               # Word exports rules as thin rectangles
                    found["lines"].append(make_line(r.x0, r.x1, (r.y0 + r.y1) / 2, "drawn"))
                elif r.width <= 2.5 and r.height >= 6:
                    found["verticals"].append(list(r))
                elif r.width >= 60 and r.height >= 18 and path.get("color") is not None:
                    found["boxes"].append(list(r))                              # an outlined box
            elif kind == "c":                                                   # a curve: maybe someone's ink
                found["curves"].append(list(path["rect"]))
    return found


def underline_y(y0, y1, is_dots):
    """Where the line of a typed blank is: underscores sit at the bottom of the box, dots a little higher."""
    return y1 - (0.2 * (y1 - y0) if is_dots else 1)


def text_lines(page, blanks):
    """Lines typed as text: runs of underscores or dots, including ones glued to a label ('By:____')."""
    found = [make_line(b["x0"], b["x1"], underline_y(b["y0"], b["y1"], b["dots"]), "text")
             for b in blanks if b["x1"] - b["x0"] >= MIN_LINE_WIDTH]
    for run, is_dots in (("_____", False), (".....", True)):
        for r in page.search_for(run):
            if r.width >= MIN_LINE_WIDTH:
                found.append(make_line(r.x0, r.x1, underline_y(r.y0, r.y1, is_dots), "text"))
    return found


def merge_lines(lines):
    """One line per place: a drawn rule under a run of underscores is the same line."""
    merged = []
    for line in sorted(lines, key=lambda l: (l["y"], l["x0"])):
        for m in merged:
            if abs(m["y"] - line["y"]) <= 3 and line["x0"] <= m["x1"] + 3 and line["x1"] >= m["x0"] - 3:
                m["x0"], m["x1"] = min(m["x0"], line["x0"]), max(m["x1"], line["x1"])
                if line["source"] == "drawn":
                    m["y"], m["source"] = line["y"], "drawn"
                break
        else:
            merged.append(dict(line))
    return merged


def is_text_underline(line, words):
    """A rule that sits right under words is underlined text, not a signature line."""
    covered = sum(overlap(w["x0"], w["x1"], line["x0"], line["x1"]) for w in words
                  if not w.get("field") and w["y0"] - 1 <= line["y"] <= w["y1"] + 2.5)
    return covered >= 0.5 * (line["x1"] - line["x0"])


def is_table_border(line, verticals):
    """A rule whose end touches a vertical rule is part of a table or box."""
    for v in verticals:
        if v[1] - 2 <= line["y"] <= v[3] + 2:
            for end in (line["x0"], line["x1"]):
                if v[0] - 3 <= end <= v[2] + 3:
                    return True
    return False


def why_hidden(span, page, dark_fills):
    """Why a reader can't see a span of text ('invisible', 'tiny', 'off the page', 'white'), or None."""
    x0, y0, x1, y1 = span["bbox"]
    far = max(page.rect.width, page.rect.height)
    if span.get("alpha", 255) == 0:
        return "invisible"
    if span["size"] < 3.5:
        return "tiny"
    if x1 < 0 or y1 < 0 or x0 > far or y0 > far:
        return "off the page"
    color = span["color"]                                   # 0xRRGGBB
    red, green, blue = (color >> 16) & 255, (color >> 8) & 255, color & 255
    if min(red, green, blue) >= 235:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if not any(f[0] <= cx <= f[2] and f[1] <= cy <= f[3] for f in dark_fills):
            return "white"                                  # white text, and not on a dark background
    return None


def hidden_text(page, dark_fills):
    """Text a reader can't see: invisible, white on white, tiny or off the page."""
    found = []
    blocks = page.get_text("dict")["blocks"]
    for span in (span for block in blocks for line in block.get("lines", []) for span in line["spans"]):
        text = span["text"].strip()
        if len(text) < 2:
            continue
        why = why_hidden(span, page, dark_fills)
        if why:
            found.append({"text": text[:80], "why": why})
    return found


def unrotated_size(page):
    """Page width and height before rotation: PyMuPDF reports positions in the unrotated page."""
    width, height = page.rect.width, page.rect.height
    if page.rotation in (90, 270):
        width, height = height, width
    return width, height


def read_page(doc, index):
    """Everything the rules need about one page."""
    page = doc[index]
    words, blanks = read_words(page)
    fields = read_fields(page)
    tag_field_words(words, fields)
    drawings = read_drawings(page)
    lines = merge_lines(drawings["lines"] + text_lines(page, blanks))
    lines = [l for l in lines if not is_text_underline(l, words) and not is_table_border(l, drawings["verticals"])]
    images = [list(info["bbox"]) for info in page.get_image_info()]
    width, height = unrotated_size(page)
    # A scan: almost no text, and one image covering most of the page.
    scanned = len(words) < 10 and any((i[2] - i[0]) * (i[3] - i[1]) > 0.6 * width * height for i in images)
    return {
        "number": index + 1, "width": width, "height": height, "rotation": page.rotation,
        "words": words, "blanks": blanks, "lines": lines, "fields": fields, "boxes": drawings["boxes"],
        "images": images, "annots": [{"type": a.type[1], "rect": list(a.rect)} for a in page.annots()],
        "curves": drawings["curves"], "hidden": hidden_text(page, drawings["dark_fills"]), "scanned": scanned,
    }


def page_info(doc, index):
    """A page's number, size and rotation, before its content is read."""
    page = doc[index]
    width, height = unrotated_size(page)
    return {"number": index + 1, "width": round(width, 1), "height": round(height, 1),
            "rotation": page.rotation, "scanned": False}


def page_summary(page):
    """What the job record keeps about a page that was read."""
    return {"number": page["number"], "width": round(page["width"], 1), "height": round(page["height"], 1),
            "rotation": page["rotation"], "scanned": page["scanned"]}


# =============================================================================
# Step 2, Detect: the structure of a page (rows, labels, headers and footers)
# =============================================================================

def group_rows(words):
    """Group words into visual rows by their bottom edge."""
    groups = []
    for w in sorted(words, key=lambda w: (w["y1"], w["x0"])):
        if groups and w["y1"] - groups[-1][0]["y1"] <= ROW_TOLERANCE:      # close to the row's first word
            groups[-1].append(w)
        else:
            groups.append([w])
    rows = []
    for group in groups:
        ws = sorted(group, key=lambda w: w["x0"])
        rows.append({"words": ws, **bounds(ws), "text": " ".join(w["text"] for w in ws)})
    return rows


def mark_boilerplate(pages):
    """Text repeated at the same height on most pages is a header or footer. Rules ignore it."""
    if len(pages) < 3:
        return
    seen = {}       # the row's text, digits replaced by '#' -> [(page number, row), ...]
    for p in pages:
        for row in group_rows(p["words"]):
            key = re.sub(r"\d+", "#", norm(row["text"]))
            if len(key) >= 3:
                seen.setdefault(key, []).append((p["number"], row))
    needed = max(3, round(len(pages) * 0.6))
    for hits in seen.values():
        page_numbers = {number for number, _ in hits}
        heights = [row["y1"] for _, row in hits]
        if len(page_numbers) >= needed and max(heights) - min(heights) <= 24:
            for _, row in hits:
                for w in row["words"]:
                    w["boiler"] = True


def page_text(rows):
    return "\n".join(r["text"] for r in rows)


def classify(text):
    """What a short piece of text is: a signature label ('sign', 'by'), 'date', 'never' (a witness) or 'text'."""
    toks = tokens(text)
    if not toks or "whereof" in toks:
        return "text"
    if "date" in toks:
        i = toks.index("date")
        if len(toks) > MAX_LABEL_WORDS or (i > 0 and toks[i - 1] in NOT_SIGNING_DATE_WORDS):
            return "text"               # a sentence, or 'Effective Date'
        return "date"
    is_label = len(toks) <= MAX_LABEL_WORDS or (toks[0] == "signed" and len(toks) <= 12)
    if not is_label:
        return "text"
    words = set(toks)
    if words & NEVER_WORDS:
        return "never"
    if words & NOT_A_SPOT_WORDS:
        return "text"
    if words & SIGN_WORDS:
        return "sign"
    if toks[0] == "by" and len(toks) <= 3:
        return "by"
    return "text"


def is_field_label(text):
    toks = tokens(text)
    return bool(toks) and toks[0] in FIELD_WORDS


def make_segments(rows):
    """Split each row into separate labels where there is a wide gap ('Signature ____ Date ____').

    A form field's value is always a segment of its own, of kind 'value'.
    """
    groups = []
    for row in rows:
        current = [row["words"][0]]
        for w in row["words"][1:]:
            if w["x0"] - current[-1]["x1"] > SEGMENT_GAP or w.get("field") != current[-1].get("field"):
                groups.append(current)
                current = [w]
            else:
                current.append(w)
        groups.append(current)
    segments = []
    for ws in groups:
        text = " ".join(w["text"] for w in ws)
        segments.append({**bounds(ws), "text": text, "kind": "value" if ws[0].get("field") else classify(text)})
    return segments


def row_kind(row, page_width):
    """How a row behaves in a signature block: a field, an entity line, a header, or body text."""
    toks = tokens(row["text"])
    if not toks:
        return "blank"
    if len(toks) >= 14 or (len(toks) >= 8 and row["x1"] - row["x0"] >= 0.6 * page_width):
        return "long"
    if toks[0] in FIELD_WORDS:
        return "field"
    if toks[0] in ENTITY_WORDS and len(toks) >= 2:
        return "entity"
    return "text"


def column_rows(rows, col):
    """The parts of each row that fall inside a column (for side-by-side signature blocks)."""
    return group_rows([w for r in rows for w in r["words"] if col[0] <= center_x(w) <= col[1]])


def is_paragraph_tail(row, rows, page_width):
    """'Employee.' alone on a row is the end of a paragraph, not a header."""
    return any(row_kind(other, page_width) == "long" and 0 <= row["y0"] - other["y1"] <= 6
               and overlap(other["x0"], other["x1"], row["x0"], row["x1"]) > 0 for other in rows)


# =============================================================================
# Step 2, Detect: signature spots
# =============================================================================

SPOT_LABELS = ("sign", "by", "never")       # label kinds that mark a spot ('never': one we must not sign)


def spot_kind(label):
    return "never" if label["kind"] == "never" else "signature"


def label_left_of(line, segments):
    """The label on the same row, just left of the line ('Client's Signature ______')."""
    best = None
    for seg in segments:
        if seg["kind"] == "value" or abs(seg["y1"] - line["y"]) > 5:
            continue
        gap = line["x0"] - seg["x1"]
        if -3 <= gap <= 60 and (best is None or seg["x1"] > best["x1"]):
            best = seg
    return best


def label_below(line, segments, taken):
    """A label printed just under the line ('______' then 'Signature')."""
    best = None
    for seg in segments:
        if seg["kind"] == "value" or id(seg) in taken:
            continue
        if line["y"] - 2 <= seg["y0"] <= line["y"] + 14 and line["x0"] - 8 <= seg["x0"] <= line["x1"] - 10:
            if best is None or seg["y0"] < best["y0"]:
                best = seg
    return best


def line_role(label):
    """What a line's label says the line is for: 'spot', 'date', 'field', or None (plain text, or no label)."""
    if label is None:
        return None
    if label["kind"] in SPOT_LABELS:
        return "spot"
    if label["kind"] == "date":
        return "date"
    if is_field_label(label["text"]):
        return "field"
    return None


def free_line_below(seg, lines, rows):
    """An unlabeled line within 60 pt under a 'Signature:' label, with nothing printed between."""
    best = None
    for line in lines:
        if line["role"] != "free":
            continue
        gap = line["y"] - seg["y1"]
        if 2 < gap <= 60 and line["x0"] <= seg["x0"] + 15 and line["x1"] >= seg["x0"] + 60:
            between = [r for r in rows if r["y0"] > seg["y1"] - 1 and r["y1"] < line["y"] - 1
                       and overlap(r["x0"], r["x1"], line["x0"], line["x1"]) > 0]
            if not between and (best is None or line["y"] < best["y"]):
                best = line
    return best


def space_right_is_free(seg, page, needed=130):
    """Nothing printed to the right of the label on its row for `needed` points."""
    zone = [seg["x1"] + 2, seg["y0"] - 4, seg["x1"] + needed, seg["y1"] + 2]
    if zone[2] > page["width"] - 18:
        return False
    taken = [rect_of(w) for w in page["words"]] + [f["rect"] for f in page["fields"]]
    return not any(intersects(zone, r) for r in taken)


def line_coords(line):
    return [round(line["x0"], 1), round(line["y"], 1), round(line["x1"], 1)]


def new_spot(page, shape, label, kind, line=None, rect=None):
    """A place where a signature could go.

    'area' is the box outlined in previews and AI images. 'top' and 'bottom' are the spot's
    vertical extent (its label included) and 'col' its column: they locate the rest of its block.
    """
    if line:
        area = [line["x0"], line["y"] - 26, line["x1"], line["y"] + 3]
    elif rect:
        area = list(rect)
    else:  # a label with blank space to its right
        area = [label["x1"] + 4, label["y0"] - 18, min(label["x1"] + 190, page["width"] - 18), label["y1"] + 4]

    if shape == "label_then_line":
        top, bottom = min(label["y0"], line["y"] - 12), max(label["y1"], line["y"] + 2)
    elif shape == "line_then_label_below":
        top, bottom = line["y"] - 12, label["y1"]
    elif shape == "label_then_line_below":
        top, bottom = label["y0"], line["y"] + 2
    elif shape == "label_blank":
        top, bottom = label["y0"] - 2, label["y1"] + 2
    else:  # a signature field or a box
        top, bottom = area[1], area[3]
        if label:
            top, bottom = min(top, label["y0"]), max(bottom, label["y1"])

    left, right = area[0], area[2]
    if label:
        left, right = min(left, label["x0"]), max(right, label["x1"])
    return {
        "page": page["number"], "kind": kind, "shape": shape,
        "label": label["text"] if label else "", "label_rect": rounded(rect_of(label)) if label else None,
        "line": line_coords(line) if line else None,
        "area": rounded(area), "top": top, "bottom": bottom, "col": [left - 8, right + 8],
    }


def line_spots(page, segments, used):
    """Spots on lines, each labelled on its left ('Signature: ____') or under it.

    Also records what every line is for in line["role"]: 'spot', 'date', 'field' or 'free'.
    """
    lines = page["lines"]
    # Labels on the left first, so 'Name: ____' labels the name line and is never taken as the
    # label printed under the line above it.
    for line in lines:
        line["left"] = label_left_of(line, segments)
    left_labels = {id(line["left"]) for line in lines if line["left"]}
    spots = []
    for line in lines:
        line["below"] = label_below(line, segments, left_labels)
        # The label on the left decides; if it is plain text, the label under the line does.
        if line_role(line["left"]):
            label, shape = line["left"], "label_then_line"
        else:
            label, shape = line["below"], "line_then_label_below"
        line["role"] = line_role(label) or "free"
        if line["role"] == "spot":
            spots.append(new_spot(page, shape, label, spot_kind(label), line=line))
            used.add(id(label))
    return spots


def nearest_label(rect, segments):
    """The label next to a form field: same row on the left, else just above or below."""
    best, best_distance = None, float("inf")
    for seg in segments:
        if seg["kind"] not in ("sign", "by", "never", "text"):          # not a date label or a field value
            continue
        same_row = abs(seg["y1"] - rect[3]) <= 8 and 0 <= rect[0] - seg["x1"] <= 80
        above = 0 <= rect[1] - seg["y1"] <= 20 and overlap(seg["x0"], seg["x1"], rect[0], rect[2]) > 0
        below = 0 <= seg["y0"] - rect[3] <= 14 and overlap(seg["x0"], seg["x1"], rect[0], rect[2]) > 0
        if same_row or above or below:
            distance = abs(seg["y1"] - rect[3]) + abs(seg["x0"] - rect[0]) * 0.1
            if distance < best_distance:
                best, best_distance = seg, distance
    return best


def field_spots(page, segments, used):
    """Empty signature form fields (PDF fields made for signing), each with the label next to it."""
    spots = []
    for f in page["fields"]:
        if f["kind"] != "signature" or f.get("signed"):
            continue
        label = nearest_label(f["rect"], segments)
        spot = new_spot(page, "signature_field", label, "signature", rect=f["rect"])
        spot["field_xref"] = f["xref"]
        spots.append(spot)
        if label:
            used.add(id(label))
    return spots


def labels_box(seg, box):
    """True when the label is printed inside the box or just above it."""
    inside = box[0] - 1 <= seg["x0"] and seg["x1"] <= box[2] + 1 and box[1] - 1 <= seg["y0"] and seg["y1"] <= box[3] + 1
    above = box[1] - 14 <= seg["y1"] <= box[1] + 2 and overlap(seg["x0"], seg["x1"], box[0], box[2]) > 0
    return inside or above


def box_spots(page, segments, used):
    """Outlined boxes with a signature label inside or just above."""
    spots = []
    for box in page["boxes"]:
        label = next((seg for seg in segments
                      if seg["kind"] in ("sign", "by") and id(seg) not in used and labels_box(seg, box)), None)
        if label:
            spots.append(new_spot(page, "signature_box", label, "signature", rect=box))
            used.add(id(label))
    return spots


def lone_label_spots(page, segments, rows, used):
    """Signature labels with no line of their own: a free line further down, or blank space to the right."""
    spots = []
    for seg in segments:
        if id(seg) in used or seg["kind"] not in SPOT_LABELS:
            continue
        line = free_line_below(seg, page["lines"], rows)
        if line:
            spots.append(new_spot(page, "label_then_line_below", seg, spot_kind(seg), line=line))
            line["role"] = "spot"
            continue
        text = seg["text"].strip()
        if seg["kind"] == "by" and not text.endswith(":"):
            continue                    # 'By' without a colon or a line is not a place to sign
        ends_like_label = text.endswith(":") or tokens(text)[-1] in SIGN_WORDS
        if ends_like_label and space_right_is_free(seg, page):
            spots.append(new_spot(page, "label_blank", seg, spot_kind(seg)))
    return spots


SHAPE_PRIORITY = {"signature_field": 0, "label_then_line": 1, "line_then_label_below": 1,
                  "label_then_line_below": 1, "signature_box": 2, "label_blank": 3}


def drop_duplicate_spots(spots):
    """Keep one spot per place. A form field beats a line, a line a box, and a box blank space."""
    kept = []
    for spot in sorted(spots, key=lambda s: SHAPE_PRIORITY[s["shape"]]):
        a = spot["area"]
        own_area = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
        if not any(other["page"] == spot["page"] and overlap_area(a, other["area"]) > 0.3 * own_area
                   for other in kept):
            kept.append(spot)
    return sorted(kept, key=lambda s: (s["page"], s["top"], s["area"][0]))


def find_spots(page, rows):
    """Find every signature spot and 'Date' label on a page. Returns (spots, date labels)."""
    segments = make_segments(rows)
    used = set()            # ids of the labels that already have a spot
    # The order matters: each kind of spot only takes labels the kinds before it left unused.
    spots = line_spots(page, segments, used)
    spots += field_spots(page, segments, used)
    spots += box_spots(page, segments, used)
    spots += lone_label_spots(page, segments, rows, used)
    date_segments = [s for s in segments if s["kind"] == "date"]
    return drop_duplicate_spots(spots), date_segments


# =============================================================================
# Step 2, Detect: each spot's block (header, name, title) and its date
# =============================================================================

def split_between(rows, upper, lower, page_width):
    """Rows between two stacked spots: trailing fields of the upper block, then the lower block's header."""
    col = [min(upper["col"][0], lower["col"][0]), max(upper["col"][1], lower["col"][1])]
    between = [r for r in column_rows(rows, col) if r["y0"] >= upper["bottom"] - 1 and r["y1"] <= lower["top"] + 1]
    # The upper block's own field rows (Name, Title...) come first...
    split = 0
    while split < len(between) and row_kind(between[split], page_width) in ("field", "entity", "blank"):
        split += 1
    # ...then the first row that sits closer to the lower block than to the row above starts its header.
    while split < len(between):
        row = between[split]
        if row_kind(row, page_width) != "long":
            edge_above = between[split - 1]["y1"] if split > 0 else upper["bottom"]
            edge_below = between[split + 1]["y0"] if split + 1 < len(between) else lower["top"]
            if row["y0"] - edge_above + 2 >= edge_below - row["y1"]:
                break
        split += 1
    upper_rows = [r for r in between[:split] if row_kind(r, page_width) != "long"]
    lower_rows = [r for r in between[split:] if row_kind(r, page_width) != "long" and r["y0"] >= lower["top"] - HEADER_REACH]
    return upper_rows, lower_rows


def header_above(rows, spot, page_width):
    """Short rows just above a spot that has no spot above it ('PARTY A', 'ACME ANALYTICS INC.')."""
    candidates = [r for r in column_rows(rows, spot["col"]) if spot["top"] - HEADER_REACH <= r["y0"] and r["y1"] <= spot["top"] + 1]
    result, edge = [], spot["top"]
    for row in sorted(candidates, key=lambda r: -r["y1"]):          # from the spot upwards
        if row_kind(row, page_width) in ("long", "field"):
            break
        if edge - row["y1"] > (30 if not result else 20):
            break
        if is_paragraph_tail(row, rows, page_width):
            break
        result.insert(0, row)
        edge = row["y0"]
    return result


def rows_below(rows, spot, page_width):
    """Name, Title and Date rows under a spot that has no spot below it."""
    candidates = [r for r in column_rows(rows, spot["col"]) if spot["bottom"] - 1 <= r["y0"] <= spot["bottom"] + BLOCK_REACH]
    result, edge = [], spot["bottom"]
    for row in sorted(candidates, key=lambda r: r["y0"]):           # from the spot downwards
        if row_kind(row, page_width) not in ("field", "entity"):
            break
        if row["y0"] - edge > 30:
            break
        result.append(row)
        edge = row["y1"]
    return result


def stacked_neighbours(spot, spots):
    """The nearest other spots straight above and below this one in its column (None when there is none)."""
    same_col = [o for o in spots if o is not spot and overlap(o["col"][0], o["col"][1], spot["col"][0], spot["col"][1]) > 0]
    above = [o for o in same_col if o["bottom"] <= spot["top"] + 2]
    below = [o for o in same_col if o["top"] >= spot["bottom"] - 2]
    return (max(above, key=lambda o: o["bottom"], default=None),
            min(below, key=lambda o: o["top"], default=None))


def tail_of_page(rows, page_width):
    """The last text rows at the bottom of a page (up to two)."""
    if not rows:
        return ""
    bottom = rows[-1]["y1"]
    tail = [r for r in rows if r["y0"] >= bottom - 40 and row_kind(r, page_width) == "text"][-2:]
    return " ".join(r["text"] for r in tail)


def build_blocks(page, rows, spots, previous_rows):
    """Give each spot its block: the header above, its own label rows, and the field rows below.

    Sets 'header', 'own_text', 'below', 'block_text' and 'borrowed_header' on each spot, and keeps
    the block's rows in '_block_rows' for find_date_for.
    """
    width = page["width"]
    for spot in spots:
        upper, lower = stacked_neighbours(spot, spots)
        header = split_between(rows, upper, spot, width)[1] if upper else header_above(rows, spot, width)
        trailing = split_between(rows, spot, lower, width)[0] if lower else rows_below(rows, spot, width)
        own = [r for r in column_rows(rows, spot["col"]) if r["y1"] > spot["top"] and r["y0"] < spot["bottom"]]
        spot["header"] = " ".join(r["text"] for r in header)
        spot["own_text"] = " ".join(r["text"] for r in own)
        spot["below"] = " ".join(r["text"] for r in trailing)
        spot["block_text"] = " ".join(t for t in (spot["header"], spot["own_text"], spot["below"]) if t).strip()
        spot["_block_rows"] = header + own + trailing
        # A spot at the top of the page with nothing above it may belong to a block split over the
        # page break, its header at the bottom of the previous page. That text is kept apart: it
        # can also be a notice address, so it is weak evidence (see judge_spot).
        continues_block = not header and not upper and spot["top"] < 140
        spot["borrowed_header"] = tail_of_page(previous_rows, width) if continues_block else ""


def date_target(seg, page):
    """What to fill for a 'Date' label: a form field next to it, its line, or blank space after it."""
    for f in page["fields"]:
        r = f["rect"]
        if f["kind"] == "text" and seg["x1"] - 6 <= r[0] <= seg["x1"] + 60 and r[1] < seg["y1"] + 3 and r[3] > seg["y0"] - 14:
            return {"kind": "field", "area": rounded(r), "field_name": f["name"], "field_xref": f["xref"],
                    "value": f["value"]}
    for line in page["lines"]:
        if line.get("left") is seg or line.get("below") is seg:
            return {"kind": "line", "area": rounded([line["x0"], line["y"] - 12, line["x1"], line["y"] + 2]),
                    "line": line_coords(line)}
    return {"kind": "blank", "area": rounded([seg["x1"] + 4, seg["y0"], seg["x1"] + 84, seg["y1"]]),
            "label_rect": rounded(rect_of(seg))}


def find_date_for(spot, page, date_segments):
    """The date that belongs to a signature spot: on its row to the right, or in its block below."""
    if spot["line"]:
        row_y, right_edge = spot["line"][1], spot["line"][2]
    else:
        row_y, right_edge = spot["area"][3] - 4, spot["area"][2]
    candidates = []         # (distance, 'Date' label)
    for seg in date_segments:
        same_row = abs(seg["y1"] - row_y) <= 6 and right_edge - 5 <= seg["x0"] <= right_edge + 250
        in_block = seg["y0"] >= spot["bottom"] - 2 and any(
            abs(seg["y1"] - r["y1"]) <= 2 and seg["x0"] >= r["x0"] - 2 and seg["x1"] <= r["x1"] + 2
            for r in spot["_block_rows"])
        if same_row or in_block:
            candidates.append((abs(seg["y1"] - row_y) + abs(seg["x0"] - right_edge) * 0.2, seg))
    if spot["line"]:
        # A date line on the same row whose 'Date' label is printed under it.
        for line in page["lines"]:
            if (line["role"] == "date" and line.get("below") and abs(line["y"] - row_y) <= 4
                    and right_edge - 5 <= line["x0"] <= right_edge + 250):
                candidates.append((abs(line["x0"] - right_edge) * 0.2, line["below"]))
    if not candidates:
        return None
    seg = min(candidates, key=lambda c: c[0])[1]
    target = date_target(seg, page)
    target.update(page=page["number"], label=seg["text"], label_rect=rounded(rect_of(seg)))
    return target


# =============================================================================
# Step 2, Detect: who owns each block
# =============================================================================

def ownership(owner, evidence, reason, role=None):
    """A judgement of who owns a block, in the keys a spot stores it under.

    owner is 'ours', 'theirs', 'never' or 'unknown'. evidence says how sure: 'name' (your name or
    signer is in the block), 'role' (your role, defined next to your name), 'unnamed_party',
    'usual_role', 'empty_block' or 'page_break' (see policy.EVIDENCE_TEXT).
    """
    return {"owner": owner, "evidence": evidence, "owner_reason": reason, "role": role}


def judge_owner(spot, parties, settings):
    """Who owns a block, judged on its own text: by the names in it first, then by the roles."""
    text = spot["block_text"] + " " + spot["label"]
    if spot["kind"] == "never" or NEVER_WORDS & set(tokens(text)):
        return ownership("never", None, "the block is for a witness, notary or guarantor")
    names = ([("ours", "your company's name", n) for n in company_names(settings) + parties["extra_aliases"]]
             + [("ours", "your signer's name", settings["signer_name"])]
             + [("theirs", "the other party's name", n) for n in parties["other_names"]])
    for owner, whose, name in names:
        hit = find_phrase(text, name)
        if hit:
            return ownership(owner, "name", f"the block shows {whose} '{hit.group(0)}'")
    roles = roles_in_text(text, parties["roles"] + settings["usual_roles"])
    if parties["our_role"]:
        return judge_by_contract_role(roles, parties)
    return judge_by_usual_role(roles, parties, settings)


def judge_by_contract_role(roles, parties):
    """Judge a block by the roles it names, when the contract says which role is yours."""
    our_role = parties["our_role"]
    mine = [r for r in roles if norm(r) == norm(our_role)]
    others = [r for r in roles if norm(r) != norm(our_role)]
    if mine and others:
        return ownership("unknown", None, f"the block names both '{our_role}' and '{others[0]}'")
    if mine and parties["our_name_found"]:
        return ownership("ours", "role", f"the label names your role in this contract, '{our_role}'", our_role)
    if mine:
        return ownership("ours", "unnamed_party", f"the label names '{our_role}', the only party whose name the "
                         "contract leaves blank (the other party is named)", our_role)
    if others:
        return ownership("theirs", "role", f"the label names the other party's role, '{others[0]}'", others[0])
    return ownership("unknown", None, "the block names no party or role")


def judge_by_usual_role(roles, parties, settings):
    """Judge a block by the roles it names, when the contract doesn't say which role is yours.

    One of your usual roles counts as yours, unless the contract gives it to a named other party.
    """
    usual = [norm(r) for r in settings["usual_roles"]]
    taken = [norm(r) for r in parties["named_roles"]]
    mine = [r for r in roles if norm(r) in usual and norm(r) not in taken]
    others = [r for r in roles if r not in mine]
    if len(mine) == 1 and not others:
        return ownership("ours", "usual_role", f"your company's name was not found; '{mine[0]}' is one of the "
                         "roles you usually have", mine[0])
    if len(mine) > 1:
        return ownership("unknown", None, f"the block names two of your usual roles ({', '.join(mine)})")
    if others:
        return ownership("theirs", "role", f"the label names '{others[0]}', a role you don't usually have", others[0])
    return ownership("unknown", None, "the block names no party or role")


def resolve_owners(spots):
    """Two rules that look at all blocks together, after each block was judged on its own.

    1. Your usual roles must point to one role only. If one block says 'Disclosing Party' and
       another 'Receiving Party' and both are roles you usually have, nobody can tell which side
       you are, so none of them counts as yours.
    2. The empty blocks left: when the other party already signed some blocks, the blocks that are
       still empty and name no party are yours, but only if there are exactly as many of them as
       blocks the other party signed. A block the client forgot to sign is then never mistaken
       for yours, which matters when a contract has several blocks per party.
    """
    usual = [s for s in spots if s["evidence"] == "usual_role"]
    usual_roles = sorted({norm(s["role"]) for s in usual})
    if len(usual_roles) > 1:
        for s in usual:
            s.update(ownership("unknown", None, f"different blocks name different roles you usually have "
                               f"({', '.join(usual_roles)}), so it isn't clear which side you are"))

    signature_spots = [s for s in spots if s["kind"] == "signature"]
    signed = [s for s in signature_spots if not s["empty"] and s["owner"] != "ours"]
    empty_unknown = [s for s in signature_spots if s["empty"] and s["owner"] == "unknown"]
    already_ours = [s for s in signature_spots if s["owner"] == "ours"]
    # Your blocks (already found plus the empty unnamed ones) must match the blocks the other party signed.
    if signed and empty_unknown and len(already_ours) + len(empty_unknown) == len(signed):
        for s in empty_unknown:
            s.update(ownership("ours", "empty_block", f"the other party already signed {len(signed)} block(s); this "
                               f"is one of the {len(empty_unknown)} block(s) still empty, and it names no party"))


def roles_in_text(text, roles):
    """Roles that appear in a text as whole words. Longer roles first, so 'Service Provider' isn't also 'Provider'."""
    found, remaining = [], text
    for role in sorted({r for r in roles if r}, key=len, reverse=True):
        pattern = phrase_pattern(role)
        if pattern and re.search(pattern, remaining, flags=re.IGNORECASE):
            found.append(role)
            remaining = re.sub(pattern, " ", remaining, flags=re.IGNORECASE)
    return found


# =============================================================================
# Step 2, Detect: the document type and the parties (from the opening pages)
# =============================================================================

def rules_doc_type(first_page_text):
    """The document type whose pattern appears first on page 1 (the title comes first)."""
    found = []      # (position, document type)
    for doc_type, pattern in DOC_TYPE_PATTERNS:
        m = re.search(pattern, first_page_text, flags=re.IGNORECASE)
        if m:
            found.append((m.start(), doc_type))
    return min(found, key=lambda f: f[0])[1] if found else "other"


def quoted_terms(text):
    """Terms in quotes, with line breaks inside them squeezed to one space ('Service\\nProvider')."""
    found = re.findall(r"[\u201c\"\u2018]([^\u201d\"\u2019]{2,40})[\u201d\"\u2019]", text)
    return [re.sub(r"\s+", " ", term).strip() for term in found]


def bracket_after(text):
    """The bracket that directly follows a name: 'Acme Inc., a Delaware corporation (the "Provider")'.

    It only counts when nothing between the name and the bracket points at another party:
    in 'Acme Inc. and Foo LLC (each a "Party")' or 'Acme Inc. ... between Foo LLC ("Client")'
    the bracket belongs to someone else.
    """
    m = re.match(r"([^()]{0,160}?)\(([^()]{0,200})\)", text)
    if not m or re.search(r"\bbetween\b|\band\b|;", m.group(1), flags=re.IGNORECASE):
        return None
    return m.group(2)


def role_after(text, known):
    """The quoted role in the bracket right after a name."""
    for term in quoted_terms(bracket_after(text) or ""):
        if norm(term) in known:
            return term
    return None


def role_before(text, known):
    """A role label just before a name ('Service Provider: Acme Inc.')."""
    m = re.search(r"([A-Za-z][A-Za-z ]{1,30}?)\s*(?:#?\s*\d+)?\s*:\s*$", text)
    if m and norm(m.group(1)) in known:
        return m.group(1).strip()
    return None


def is_short_name(term, legal_name):
    """'Acme', 'Acme Analytics' and 'AA' are short names of 'Acme Analytics Inc.'; 'Party', 'Foo' and 'Inc' are not."""
    words = re.findall(r"[a-z0-9]+", legal_name.lower())
    core = [w for w in words if w not in LEGAL_SUFFIXES] or words
    term_words = re.findall(r"[a-z0-9]+", term.lower())
    if not term_words:
        return False
    if all(w in words for w in term_words) and any(w in core for w in term_words):
        return True
    initials = "".join(w[0] for w in core)
    return len(initials) >= 2 and "".join(term_words) in (initials, "".join(w[0] for w in words))


def clean_name(text):
    """Tidy a party name cut out of the preamble; empty if the template left it blank.

    'Foo LLC, a Delaware company' -> 'Foo LLC'. 'with a mailing address of' (the name was a
    blank line) -> ''. A real name starts with a capital letter or a digit.
    """
    stop = re.search(r",|\(|\bwith\b|\ba company\b|\bhaving\b|\bresiding\b|\blocated\b|\borgani[sz]ed\b"
                     r"|\bincorporated\b|\ban? [a-z]+ (?:corporation|company|limited)", text)
    if stop:
        text = text[:stop.start()]
    text = re.sub(r"\s+", " ", text).strip(" :;.-\"'\u201c\u201d")
    if not text or not (text[0].isupper() or text[0].isdigit()):
        return ""
    return text if len(re.findall(r"[A-Za-z]", text)) >= 2 else ""


def defined_roles(opening, opening_rows, known):
    """The party roles the opening pages define: quoted in brackets ('(the "Client")') or as row labels ('Client:')."""
    roles = []

    def add(term):
        if norm(term) in known and norm(term) not in [norm(r) for r in roles]:
            roles.append(term.strip())

    for m in re.finditer(r"\(([^()]{0,200})\)", opening):
        for term in quoted_terms(m.group(1)):
            add(term)
    for row in opening_rows:
        m = re.match(r"\s*([A-Za-z][A-Za-z ]{1,30}?)\s*(?:#?\s*\d+)?\s*:", row["text"])
        if m:
            add(m.group(1))
    return roles


def find_our_company(opening, full_text, settings, known):
    """Whether your company is named, the role the contract gives it, and short names it defines for it.

    Every mention of your name in the opening pages is looked at. A role counts only from a label
    just before a mention ('Service Provider: Acme Inc.') or the bracket right after it. If two
    mentions define different roles, nobody can tell which is right, so no role is taken.
    """
    our = {"found": False, "text": None, "role": None, "role_source": None, "aliases": []}
    found_roles = {}            # norm(role) -> (role, where it was found)
    for name in company_names(settings):
        pattern = phrase_pattern(name)
        if pattern is None:
            continue
        for m in re.finditer(pattern, opening, flags=re.IGNORECASE):
            if not our["found"]:
                our.update(found=True, text=m.group(0))
            before, after = opening[max(0, m.start() - 60):m.start()], opening[m.end():]
            role, source = role_before(before, known), "label before your name"
            if not role:
                role, source = role_after(after, known), "definition right after your name"
            if role:
                found_roles.setdefault(norm(role), (role, source))
            # A short name the contract defines for you, like Acme Analytics Inc. ("Acme").
            for term in quoted_terms(bracket_after(after) or ""):
                if (norm(term) not in known and is_short_name(term, settings["company_name"])
                        and term not in our["aliases"]):
                    our["aliases"].append(term)

    if len(found_roles) == 1:
        our["role"], our["role_source"] = next(iter(found_roles.values()))
    elif len(found_roles) > 1:
        our["role_source"] = "the contract gives your company different roles: " + ", ".join(
            role for role, _ in found_roles.values())

    if not our["found"]:            # not in the opening pages, but a mention anywhere still counts
        for name in company_names(settings):
            m = find_phrase(full_text, name)
            if m:
                our.update(found=True, text=m.group(0))
                break
    return our


def name_other_parties(roles, our_role, opening, settings):
    """The other parties' names, from 'Client: Foo LLC' or 'Foo LLC (the "Client")'. Returns (names, their roles)."""
    names, named_roles = [], []
    for role in roles:
        if our_role and norm(role) == norm(our_role):
            continue
        name = ""
        m = re.search(phrase_pattern(role) + r"\s*(?:#?\s*\d+)?\s*:\s*([^\n]{2,80})", opening, flags=re.IGNORECASE)
        if m:
            name = clean_name(m.group(1))
        if not name:
            m = re.search(r"([^\n:]{2,90})\(\s*(?:the\s+|hereinafter\s+)?[\u201c\"\u2018]" + re.escape(role),
                          opening, flags=re.IGNORECASE)
            if m:
                name = clean_name(re.split(r"\bbetween\b|\band\b|\bby\b", m.group(1))[-1])
        if name and not any(find_phrase(name, n) for n in company_names(settings)):
            names.append(name)
            named_roles.append(role)
    return names, named_roles


def find_parties(all_rows, settings):
    """Parties, their roles, and which one is your company. Uses pages 1-2 plus form-field values."""
    opening_rows = [row for rows in all_rows[:2] for row in rows]
    opening = page_text(opening_rows)
    full_text = "\n".join(page_text(rows) for rows in all_rows)
    known = {norm(r) for r in KNOWN_ROLES} | {norm(r) for r in settings["usual_roles"]}

    roles = defined_roles(opening, opening_rows, known)
    our = find_our_company(opening, full_text, settings, known)
    other_names, named_roles = name_other_parties(roles, our["role"], opening, settings)

    # The unnamed party: the contract names the other side ("Client: Foo LLC") and leaves exactly
    # one party's name blank. That blank party is you. It works for any number of blocks, because
    # it identifies your role, and every block with that role is then yours.
    if not our["found"] and named_roles:
        blank = [r for r in roles if norm(r) not in [norm(n) for n in named_roles]]
        if len(blank) == 1:
            our.update(role=blank[0], role_source="the only party whose name is left blank")
    return {
        "roles": roles, "our_name_found": our["found"], "our_name_text": our["text"],
        "our_role": our["role"], "our_role_source": our["role_source"],
        "extra_aliases": our["aliases"], "other_names": other_names, "named_roles": named_roles,
        "opening_text": opening[:2500],
    }


# =============================================================================
# Step 2, Detect: where the stamp and the date go
# =============================================================================

def obstacles_for(page, skip_line=None, skip_field_xref=None):
    """Everything a stamp must not cover: words, form fields, other lines and blanks.

    `skip_line` ([x0, y, x1]) is the line the stamp sits on; `skip_field_xref` the form field it fills.
    """
    def is_skip_line(y, x0, x1, tolerance):
        return bool(skip_line) and abs(y - skip_line[1]) <= tolerance and overlap(x0, x1, skip_line[0], skip_line[2]) > 0

    found = [rect_of(w) for w in page["words"]]
    found += [f["rect"] for f in page["fields"] if f["xref"] != skip_field_xref]
    found += [[l["x0"], l["y"] - 0.5, l["x1"], l["y"] + 0.5] for l in page["lines"]
              if not is_skip_line(l["y"], l["x0"], l["x1"], 3)]
    found += [[b["x0"], b["y1"] - 2, b["x1"], b["y1"]] for b in page["blanks"]
              if not is_skip_line(b["y1"] - 1, b["x0"], b["x1"], 4)]
    return found


def is_free(rect, obstacles, page):
    """Inside the page (with a 2 pt margin) and touching no obstacle."""
    if rect[0] < 2 or rect[1] < 2 or rect[2] > page["width"] - 2 or rect[3] > page["height"] - 2:
        return False
    return not any(intersects(rect, o) for o in obstacles)


def first_free_rect(baseline, x_min, x_max, heights, drops, aspect, obstacles, page):
    """Slide the stamp along a baseline from left to right, tallest first. Returns (rect, height) or None.

    `drops` are the shares of the stamp's height that may hang below the baseline, tried in order.
    """
    for height in heights:
        width = height * aspect
        if height < 18 or width > x_max - x_min:
            continue
        for drop in drops:
            bottom = baseline + drop * height
            x = x_min
            while x + width <= x_max + 0.01:
                rect = [x, bottom - height, x + width, bottom]
                if is_free(rect, obstacles, page):
                    return rect, height
                x += 6
    return None


def place_signature(page, spot, aspect):
    """The stamp rectangle: on the line, after the label, touching no words, fields or other lines."""
    obstacles = obstacles_for(page, skip_line=spot["line"], skip_field_xref=spot.get("field_xref"))
    attempts = []           # (baseline, x_min, x_max, heights, drops), tried in order
    if spot["line"]:
        x0, y, x1 = spot["line"]
        attempts.append((y, x0 + 2, x1 - 2, SIGNATURE_HEIGHTS, DROP_RATIOS))
    elif spot["shape"] in ("signature_field", "signature_box"):
        a = spot["area"]
        room = a[3] - a[1] - 4
        heights = [h for h in SIGNATURE_HEIGHTS if h <= room] or [room]
        attempts.append((a[3] - 2, a[0] + 2, a[2] - 2, heights, [0.0]))
    else:  # label_blank: right of the label on its row, else under the label
        label = spot["label_rect"]
        label_baseline = label[3] - 0.2 * (label[3] - label[1])
        attempts.append((label_baseline, label[2] + 6, min(label[2] + 230, page["width"] - 20),
                         SIGNATURE_HEIGHTS, DROP_RATIOS))
        attempts.append((label[3] + 4 + SIGNATURE_HEIGHTS[0], label[0], min(label[0] + 230, page["width"] - 20),
                         SIGNATURE_HEIGHTS, [0.0]))
    for attempt in attempts:
        found = first_free_rect(*attempt, aspect, obstacles, page)
        if found:
            rect, height = found
            return {"fits": True, "rect": rounded(rect), "height": height}
    return {"fits": False, "reason": "the signature doesn't fit there without touching text, fields or other lines"}


def spot_is_empty(page, spot, rect):
    """Nothing is already signed or drawn where the stamp would go. Returns (empty, why not)."""
    zone = rect if rect else spot["area"]
    if any(intersects(zone, image, pad=1) for image in page["images"]):
        return False, "an image is already there"
    if any(intersects(zone, curve, pad=1) for curve in page["curves"]):
        return False, "a drawing (possibly a signature) is already there"
    for annot in page["annots"]:
        if annot["type"] in ("Ink", "Stamp", "FreeText") and intersects(zone, annot["rect"], pad=1):
            return False, f"a {annot['type']} annotation is already there"
    for f in page["fields"]:
        if f["kind"] == "signature" and f.get("signed") and intersects(zone, f["rect"], pad=1):
            return False, "a digital signature is already there"
    return True, ""


def place_date(page, date, date_text):
    """Where the date text goes. A form field gets a value; a line or blank gets text."""
    if date["kind"] == "field":
        if date.get("value"):
            return {"fits": False, "kind": "field", "reason": f"the date field already contains '{date['value']}'"}
        return {"fits": True, "kind": "field", "rect": date["area"], "field_xref": date["field_xref"],
                "field_name": date["field_name"]}
    width = pymupdf.get_text_length(date_text, fontname="helv", fontsize=DATE_FONT_SIZE)
    if date["kind"] == "line":
        x0, y, x1 = date["line"]
        x, baseline, limit = x0 + 3, y - 2, x1 + 2
        obstacles = obstacles_for(page, skip_line=date["line"])
    else:  # blank space after the label
        label = date["label_rect"]
        x, baseline = label[2] + 6, label[3] - 0.22 * (label[3] - label[1])
        limit = x + width + 1
        obstacles = obstacles_for(page)
    rect = [x, baseline - DATE_FONT_SIZE * 0.8, x + width, baseline + DATE_FONT_SIZE * 0.22]
    if x + width > limit or not is_free(rect, obstacles, page):
        return {"fits": False, "kind": "text", "reason": "the date doesn't fit next to the Date label"}
    return {"fits": True, "kind": "text", "point": [round(x, 1), round(baseline, 1)], "rect": rounded(rect)}


# =============================================================================
# Step 2, Detect: one pass over a document with the rules (no AI)
# =============================================================================

def active_content(doc):
    """JavaScript, embedded files and XFA forms: unusual in a contract, so they block auto-sign."""
    found = []
    catalog = doc.pdf_catalog()
    if doc.xref_get_key(catalog, "Names/JavaScript")[0] != "null":
        found.append("JavaScript")
    kind, value = doc.xref_get_key(catalog, "OpenAction")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    if "/JavaScript" in value or "/JS" in value:
        found.append("JavaScript that runs when the file opens")
    if doc.xref_get_key(catalog, "AcroForm/XFA")[0] != "null":
        found.append("an XFA form")
    if doc.embfile_count() > 0:
        found.append(f"{doc.embfile_count()} embedded file(s)")
    return found


def first_match(patterns, text):
    """The text matched by the first pattern that matches, or None."""
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(0)
    return None


def red_flags(pages, active, no_changes_allowed):
    """Reasons a person must look: hidden or instruction-like text, special signing rules, active content."""
    flags = []
    hidden = [h for p in pages for h in p["hidden"]]
    if hidden:
        sample = "; ".join(f"'{h['text']}' ({h['why']})" for h in hidden[:3])
        flags.append(f"Hidden text found ({len(hidden)}): {sample}")
    text = norm("\n".join(" ".join(w["text"] for w in p["words"]) for p in pages))
    injection = first_match(INSTRUCTION_PATTERNS, text)
    if injection is not None:
        flags.append(f"Text that reads like instructions to an AI: '{injection}'")
    clause = first_match(EXECUTION_PATTERNS, text)
    if clause is not None:
        flags.append(f"The contract sets a special way to sign: '{clause}'")
    flags += [f"The PDF contains {item}" for item in active]
    if no_changes_allowed:
        flags.append("The PDF's permissions don't allow changes")
    return flags


def detect_spots(pages, all_rows, date_text):
    """Every spot of the document with its block, numbered in reading order; then their dates, numbered after them.

    Returns (spots, dates). Each spot's 'date_id' points at its date (or is None).
    """
    found = []              # (spot, its date or None), in reading order
    for i, (page, rows) in enumerate(zip(pages, all_rows)):
        previous_rows = all_rows[i - 1] if i > 0 else []
        page_spots, date_segments = find_spots(page, rows)
        build_blocks(page, rows, page_spots, previous_rows)
        for spot in page_spots:
            date = find_date_for(spot, page, date_segments) if spot["kind"] == "signature" else None
            del spot["_block_rows"]
            found.append((spot, date))

    spots = [spot for spot, _ in found]
    for number, spot in enumerate(spots, start=1):
        spot["id"] = number
    dates = []
    for spot, date in found:
        spot["date_id"] = None
        if date:
            date.update(id=len(spots) + len(dates) + 1, spot_id=spot["id"],
                        placement=place_date(pages[date["page"] - 1], date, date_text))
            dates.append(date)
            spot["date_id"] = date["id"]
    return spots, dates


def judge_spot(spot, page, parties, settings, aspect):
    """Decide who owns a spot, where its stamp would go, and whether the spot is still empty."""
    spot.update(judge_owner(spot, parties, settings))
    if spot["owner"] == "unknown" and spot["borrowed_header"]:
        # Only the text at the bottom of the previous page names a party. That can be the block's
        # header, or a notice address: good enough to propose, never to auto-sign.
        borrowed = dict(spot, block_text=spot["borrowed_header"] + " " + spot["block_text"])
        judged = judge_owner(borrowed, parties, settings)
        if judged["owner"] != "unknown":
            if judged["owner"] == "ours":
                judged["evidence"] = "page_break"
            judged["owner_reason"] += " (in the text at the bottom of the previous page)"
            spot.update(judged)
    if spot["kind"] == "signature":
        spot["placement"] = place_signature(page, spot, aspect)
    else:
        spot["placement"] = {"fits": False, "reason": "witness, notary and guarantor blocks are never signed"}
    spot["empty"], spot["empty_reason"] = spot_is_empty(page, spot, spot["placement"].get("rect"))


def analyze(pdf_path, settings, aspect):
    """Parse and Detect. Returns the job record's analysis: spots, owners, placements and red flags.

    'verdict' is 'continue' when the document goes on to the AI and the checks, 'review' when a
    person must handle it, and 'rejected' when it can't be signed at all; 'reasons' says why.
    """
    started = time.time()
    result = {"rules_version": RULES_VERSION, "verdict": "continue", "reasons": [], "notes": [],
              "spots": [], "dates": [], "red_flags": [], "signatures": [], "lock_level": None,
              "mode": "content", "sign_blocked": None, "pages": [], "page_count": 0, "doc_type": "other",
              "parties": None}

    def stop(verdict, reason):
        result.update(verdict=verdict, reasons=[reason])
        return result

    size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as error:
        return stop("rejected", f"The file can't be opened as a PDF ({error})")
    with doc:
        if not doc.is_pdf:
            return stop("rejected", "The file is not a PDF")
        if doc.needs_pass:
            return stop("rejected", "The PDF is password-protected")
        result["page_count"] = doc.page_count
        if doc.page_count == 0:
            return stop("rejected", "The PDF has no pages")
        result["pages"] = [page_info(doc, i) for i in range(doc.page_count)]

        # Signatures the client already applied, and the lock level they set: read first, always,
        # so every later step (even a by-hand placement on an over-limit file) knows how to sign safely.
        state = signature_state(pdf_path, doc)
        result.update(signatures=state["signatures"], lock_level=state["lock_level"], mode=state["mode"],
                      sign_blocked=state["blocked"])
        if state["lock_level"] == 1:
            return stop("rejected", state["blocked"] + ". Ask the client for an unlocked copy, or to sign after you.")
        if state["blocked"] and state["lock_level"] != 2:
            return stop("review", state["blocked"])
        if doc.page_count > settings["max_pages"] or size_mb > settings["max_mb"]:
            return stop("review", f"Over the limit: {doc.page_count} pages, {size_mb:.1f} MB (limits: "
                                  f"{settings['max_pages']} pages, {settings['max_mb']} MB). "
                                  "Place the signature by hand or reject.")

        # Step 1, Parse
        pages = [read_page(doc, i) for i in range(doc.page_count)]
        signed_fields = {s["field"] for s in result["signatures"]}
        for p in pages:
            for f in p["fields"]:
                f["signed"] = f["kind"] == "signature" and f["name"] in signed_fields
        result["pages"] = [page_summary(p) for p in pages]
        scanned = [p["number"] for p in pages if p["scanned"]]
        result["scanned_pages"] = scanned
        if len(scanned) > len(pages) / 2:
            return stop("rejected", f"Scanned PDF: {len(scanned)} of {len(pages)} pages have no text layer. "
                                    "Scans are not supported.")
        if scanned:
            result["notes"].append(f"Pages without a text layer (scanned): {', '.join(map(str, scanned))}")

        # Step 2, Detect
        mark_boilerplate(pages)
        all_rows = [group_rows([w for w in p["words"] if not w.get("boiler")]) for p in pages]
        result["doc_type"] = rules_doc_type(page_text(all_rows[0]))
        parties = result["parties"] = find_parties(all_rows, settings)
        spots, dates = detect_spots(pages, all_rows, time.strftime(settings["date_format"]))
        for spot in spots:
            judge_spot(spot, pages[spot["page"] - 1], parties, settings, aspect)
        resolve_owners(spots)
        for spot in spots:
            spot["rules"] = spot_rules(spot)
        result["spots"], result["dates"] = spots, dates

        no_changes_allowed = not doc.permissions & (pymupdf.PDF_PERM_MODIFY | pymupdf.PDF_PERM_ANNOTATE)
        result["red_flags"] = red_flags(pages, active_content(doc), no_changes_allowed)
        result["notes"].extend(document_notes(pages, spots))
        if not spots:
            result["reasons"].append("No signature spot found")
    result["seconds"] = round(time.time() - started, 2)
    return result


EVIDENCE_SCORE = {"name": 1.0, "role": 0.9, "unnamed_party": 0.8, "empty_block": 0.6, "usual_role": 0.5,
                  "page_break": 0.3}


def spot_rules(spot):
    """The rules that fired for a spot, in the plan's RuleResult form."""
    label = spot["label"]
    shape_reason = {
        "label_then_line": f"label '{label}' with a line on the same row",
        "line_then_label_below": f"a line with '{label}' printed under it",
        "label_then_line_below": f"label '{label}' with a line further down",
        "label_blank": f"label '{label}' with blank space after it",
        "signature_field": "an empty signature form field",
        "signature_box": f"a box labelled '{label}'",
    }[spot["shape"]]
    if spot["line"]:
        x0, y, x1 = spot["line"]
        shape_reason += f" (line x {x0}-{x1} pt, y {y} pt)"
    placement = spot["placement"]
    if placement["fits"]:
        r = placement["rect"]
        placement_reason = f"stamp at x {r[0]}-{r[2]} pt, y {r[1]}-{r[3]} pt"
    else:
        placement_reason = placement["reason"]
    page = spot["page"]
    return [
        {"rule": "spot." + spot["shape"], "page": page, "match": True, "score": 0.9, "reason": shape_reason},
        {"rule": "owner." + (spot["evidence"] or spot["owner"]), "page": page, "match": spot["owner"] == "ours",
         "score": EVIDENCE_SCORE.get(spot["evidence"], 0.0), "reason": spot["owner_reason"]},
        {"rule": "placement.fit", "page": page, "match": placement["fits"],
         "score": 1.0 if placement["fits"] else 0.0, "reason": placement_reason},
    ]


def document_notes(pages, spots):
    """Things the tool leaves alone on purpose, reported so nobody is surprised."""
    notes = []
    initial_pages = [p["number"] for p in pages if any("initial" in w["text"].lower() for w in p["words"])]
    if initial_pages:
        notes.append(f"Initials are requested on {len(initial_pages)} page(s); they are left blank")
    for spot in spots:
        if spot.get("owner") == "ours" and re.search(r"\b(name|title)\b", norm(spot["below"])):
            notes.append(f"Spot {spot['id']}: the printed name and title lines are left blank")
    return notes


# =============================================================================
# Step 3, Verify: the 8-point auto-sign check
# =============================================================================

def our_spots(analysis):
    """The signature spots the rules say are yours."""
    return [s for s in analysis["spots"] if s["owner"] == "ours" and s["kind"] == "signature"]


def spot_by_id(analysis, spot_id):
    return next((s for s in analysis["spots"] if s["id"] == spot_id), None)


def date_by_id(analysis, date_id):
    return next((d for d in analysis["dates"] if d["id"] == date_id), None)


def ai_picks(ai):
    return ai["answer"]["picks"] if ai.get("ok") else []


def evidence_terms(spot, analysis, settings):
    """Words that prove a block is yours: your names, your signer, your role."""
    parties = analysis["parties"] or {}
    terms = company_names(settings) + [settings["signer_name"]] + parties.get("extra_aliases", [])
    if parties.get("our_role"):
        terms.append(parties["our_role"])
    if spot and spot.get("evidence") == "usual_role":
        terms += settings["usual_roles"]
    return [t for t in terms if t]


def quote_is_valid(quote, spot, analysis, settings):
    """The AI's quote must really be in the block it picked, and must name you or your role.

    A block found by the empty-block rule names no party, so there its quote only has to be in the block.
    """
    q = norm(quote)
    if len(q) < 3 or not spot:
        return False, "no quote"
    score = match_score(q, norm(spot["block_text"] + " " + spot["label"]))
    if score < 90:
        return False, f"the quote '{quote}' is not in that block (match {score:.0f}%)"
    names_you = any(find_phrase(quote, t) for t in evidence_terms(spot, analysis, settings))
    if spot.get("evidence") != "empty_block" and not names_you:
        return False, f"the quote '{quote}' doesn't name your company or role"
    return True, f"quote '{quote}' found in the block"


def choose_proposal(analysis, ai):
    """What to sign: every block the rules say is yours; if they found none, the AI's picks the rules don't object to."""
    ours = our_spots(analysis)
    if ours:
        return [{"spot": s["id"], "date": s["date_id"]} for s in ours]
    proposal = []
    for p in ai_picks(ai):
        spot = spot_by_id(analysis, p["signature_spot"])
        if spot and spot["kind"] == "signature" and spot["owner"] not in ("theirs", "never"):
            proposal.append({"spot": spot["id"], "date": spot["date_id"]})
    return proposal


def ai_agreement(analysis, ai, ours, settings):
    """Check 4: the AI picked exactly your blocks, with the same dates and quotes from those blocks. Returns (passed, detail)."""
    rules_text = ", ".join(f"{s['id']} (date {s['date_id'] or 'none'})" for s in ours) or "none"
    if not ai.get("ok"):
        return False, f"rules: {rules_text}; AI: {ai.get('error', 'no answer')}"
    picks = ai_picks(ai)
    ai_text = ", ".join(f"{p['signature_spot']} (date {p['date_spot'] or 'none'})" for p in picks) or "none"
    same_set = bool(ours) and {s["id"] for s in ours} == {p["signature_spot"] for p in picks}
    problems = []
    for p in picks:
        spot = spot_by_id(analysis, p["signature_spot"])
        if spot in ours and p["date_spot"] != (spot["date_id"] or 0):
            problems.append(f"spot {spot['id']}: the AI chose date {p['date_spot'] or 'none'}")
        ok, why = quote_is_valid(p["quote"], spot, analysis, settings)
        if not ok:
            problems.append(f"spot {p['signature_spot']}: {why}")
    detail = f"rules: {rules_text}; AI: {ai_text}; " + ("; ".join(problems) if problems else "quotes checked")
    return same_set and not problems, detail


def run_checks(analysis, ai, settings):
    """The 8 checks from the plan, over every block that is yours. Auto-sign needs all of them."""
    checks = []

    def add(number, title, passed, detail):
        checks.append({"number": number, "title": title, "passed": bool(passed), "detail": detail})

    ours = our_spots(analysis)
    answer = ai.get("answer") if ai.get("ok") else None
    parties = analysis["parties"] or {}

    # 1. Document type in scope, and rules and AI agree on it.
    rules_type = analysis["doc_type"]
    ai_type = answer["document_type"] if answer else None
    same_type = ai_type is not None and DOC_TYPE_FAMILY.get(rules_type, rules_type) == DOC_TYPE_FAMILY.get(ai_type, ai_type)
    add(1, "Document type is in scope", rules_type in settings["auto_sign_types"] and same_type,
        f"rules: {rules_type}, AI: {ai_type or 'no answer'}; auto-sign types: {', '.join(settings['auto_sign_types'])}")

    # 2. Every block counted as yours has accepted proof. The company name is required only if Settings say so.
    allowed = STRONG_EVIDENCE if settings["require_company_name"] else NAMELESS_EVIDENCE
    if settings["require_company_name"] and not parties.get("our_name_found"):
        add(2, "Your blocks are identified", False,
            f"'{settings['company_name']}' was not found in the document, and Settings require your company's name")
    elif not ours:
        add(2, "Your blocks are identified", False, "no block is identified as yours")
    else:
        proof = "; ".join(f"spot {s['id']}: {EVIDENCE_TEXT[s['evidence']]}" for s in ours)
        weak = [s["id"] for s in ours if s["evidence"] not in allowed]
        if weak:
            proof += f"; spots {weak}: that proof isn't enough to sign without a person"
        add(2, "Your blocks are identified", not weak, proof)

    # 3. A believable number of your blocks: at least one, at most the cap.
    cap = settings["max_blocks"]
    add(3, "Number of your blocks", 1 <= len(ours) <= cap, f"found {len(ours)} block(s) for your company (cap {cap})")

    # 4. Rules and AI chose the same set of blocks and dates, and every quote checks out.
    add(4, "Rules and AI agree", *ai_agreement(analysis, ai, ours, settings))

    # 5. No red flags, and nothing about the client's signatures that forbids the change.
    flags = list(analysis["red_flags"])
    if analysis.get("sign_blocked"):
        flags.append(analysis["sign_blocked"])
    add(5, "No red flags", not flags, "; ".join(flags) if flags else "none")

    # 6. Every page you sign has a real text layer.
    pages = sorted({s["page"] for s in ours})
    scanned = [n for n in pages if analysis["pages"][n - 1]["scanned"]]
    if not pages:
        detail = "no page chosen"
    else:
        detail = f"page(s) {', '.join(map(str, pages))}" + (f"; scanned: {scanned}" if scanned else "")
    add(6, "The pages have a real text layer", bool(pages) and not scanned, detail)

    # 7. Every stamp and date fits without touching words or fields, on an upright page.
    problems = []
    for s in ours:
        if not s["placement"]["fits"]:
            problems.append(f"spot {s['id']}: {s['placement']['reason']}")
        date = date_by_id(analysis, s["date_id"])
        if date and not date["placement"]["fits"]:
            problems.append(f"date {date['id']}: {date['placement']['reason']}")
        if analysis["pages"][s["page"] - 1]["rotation"]:
            problems.append(f"spot {s['id']}: the page is rotated")
    add(7, "The stamps and dates fit", bool(ours) and not problems,
        "; ".join(problems) if problems else "all fit" if ours else "no spot chosen")

    # 8. Every spot is empty.
    filled = [f"spot {s['id']}: {s['empty_reason']}" for s in ours if not s["empty"]]
    add(8, "The spots are empty", bool(ours) and not filled,
        "; ".join(filled) if filled else "all empty" if ours else "no spot chosen")

    return {"checks": checks, "passed": all(c["passed"] for c in checks),
            "reasons": [f"Check {c['number']} failed: {c['title']}. {c['detail']}" for c in checks if not c["passed"]],
            "proposal": choose_proposal(analysis, ai),
            "rules_pick": [s["id"] for s in ours], "ai_pick": [p["signature_spot"] for p in ai_picks(ai)]}


# =============================================================================
# Step 4, Sign: where exactly the stamps go
# =============================================================================

def placement_for_spot(spot, date_id, page, analysis, aspect, date_text):
    """The stamp (and date) for one chosen spot on a freshly read page. Raises ValueError when it can't go there."""
    sig = place_signature(page, spot, aspect)
    if not sig["fits"]:
        raise ValueError(f"Spot {spot['id']}: {sig['reason']}")
    empty, why = spot_is_empty(page, spot, sig["rect"])
    if not empty:
        raise ValueError(f"Spot {spot['id']}: {why}")
    placement = {"page": spot["page"], "signature": sig["rect"], "spot_id": spot["id"], "date": None,
                 "date_id": None, "manual": False}
    if date_id:
        date = date_by_id(analysis, date_id)
        if date is None:
            raise ValueError(f"Date spot {date_id} doesn't exist")
        if date["page"] != spot["page"]:
            raise ValueError(f"Date spot {date['id']} isn't on the same page as spot {spot['id']}")
        placed = place_date(page, date, date_text)
        if not placed["fits"]:
            raise ValueError(f"Date spot {date['id']}: {placed['reason']}")
        placement["date"], placement["date_id"] = placed, date["id"]
    return placement


def placements_for_spots(analysis, choices, pdf_path, aspect, date_text):
    """Exact stamp and date positions for the chosen spots, recomputed from a fresh read of the PDF.

    `choices` is a list of {"spot": id, "date": id or None}. Raises ValueError with a reason a
    person can act on when a spot can't take a stamp.
    """
    if not choices:
        raise ValueError("No spot was chosen")
    if len({c["spot"] for c in choices}) != len(choices):
        raise ValueError("The same spot was chosen twice")
    placements, pages = [], {}          # pages: page number -> that page, read once
    with pymupdf.open(pdf_path) as doc:
        for choice in choices:
            spot = spot_by_id(analysis, choice["spot"])
            if spot is None or spot["kind"] != "signature":
                raise ValueError(f"Spot {choice['spot']} is not a signature spot")
            if spot["page"] not in pages:
                pages[spot["page"]] = read_page(doc, spot["page"] - 1)
            placements.append(placement_for_spot(spot, choice.get("date"), pages[spot["page"]], analysis, aspect, date_text))
    for a, b in combinations(placements, 2):          # two stamps must never cover each other
        if a["page"] == b["page"] and intersects(a["signature"], b["signature"], pad=0):
            raise ValueError(f"The stamps for spots {a['spot_id']} and {b['spot_id']} would overlap")
    return placements


def manual_placement(pdf_path, page_number, x, y, date_point, aspect):
    """A reviewer's own placement: the stamp centred where they clicked, the date where they clicked next."""
    with pymupdf.open(pdf_path) as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"Page {page_number} doesn't exist")
        page = doc[page_number - 1]
        if page.rotation:
            raise ValueError("Placing by hand isn't supported on rotated pages")
        width, height = page.rect.width, page.rect.height
    h = SIGNATURE_HEIGHTS[0]
    w = h * aspect
    x0 = min(max(x - w / 2, 2), width - w - 2)          # kept 2 pt inside the page
    y0 = min(max(y - h / 2, 2), height - h - 2)
    placement = {"page": page_number, "signature": rounded([x0, y0, x0 + w, y0 + h]), "spot_id": None,
                 "date": None, "manual": True}
    if date_point:
        dx, dy = date_point
        placement["date"] = {"fits": True, "kind": "text", "point": [round(dx, 1), round(dy, 1)],
                             "rect": rounded([dx, dy - 8, dx + 130, dy + 2])}
    return placement


# =============================================================================
# Step 4, Sign: stamp the signature and date on a copy, then check the copy
# =============================================================================

PRINTED_AND_LOCKED = pymupdf.PDF_ANNOT_IS_PRINT | pymupdf.PDF_ANNOT_IS_LOCKED     # flags for our annotations


def stamp_signature(page, rect, signature_png, mode, signer_name):
    """Put the signature image on the page: drawn into the page, or as a stamp annotation on client-signed PDFs."""
    rect = pymupdf.Rect(rect)
    if mode == "annotation":
        annot = page.add_stamp_annot(rect, stamp=signature_png)
        annot.set_info(title=signer_name, content="Signature")
        annot.set_flags(PRINTED_AND_LOCKED)
        annot.update()
    else:
        page.insert_image(rect, stream=signature_png, keep_proportion=True, overlay=True)


def write_date(page, date, date_text, mode, signer_name):
    """Fill in the date: as a form field's value, or as text at its point (an annotation on client-signed PDFs)."""
    if date["kind"] == "field":
        for widget in page.widgets():
            if widget.xref == date["field_xref"]:
                widget.field_value = date_text
                widget.update()
                break
        return
    x, baseline = date["point"]
    if mode == "annotation":
        width = pymupdf.get_text_length(date_text, fontname="helv", fontsize=DATE_FONT_SIZE)
        box = pymupdf.Rect(x - 1, baseline - DATE_FONT_SIZE, x + width + 3, baseline + 3)
        note = page.add_freetext_annot(box, date_text, fontsize=DATE_FONT_SIZE, fontname="helv",
                                       text_color=(0, 0, 0), border_width=0)
        note.set_info(content=date_text, title=signer_name)
        note.set_flags(PRINTED_AND_LOCKED)
        note.update()
    else:
        page.insert_text((x, baseline), date_text, fontsize=DATE_FONT_SIZE, fontname="helv", color=(0, 0, 0))


def word_positions(page):
    """Every word on a page with its position, rounded to whole points."""
    return [(w[4], round(w[0]), round(w[1])) for w in page.get_text("words")]


def page_snapshot(page):
    """What a page looks like to a reader (words, annotations, form values), for 'nothing else changed'."""
    annots = [(a.type[1], [round(v) for v in a.rect]) for a in page.annots()]
    widgets = [(w.field_name, str(w.field_value)) for w in page.widgets()]
    return json.dumps([word_positions(page), annots, widgets])


def stamp_landed(page, want, mode):
    """An image (or, on client-signed PDFs, a stamp annotation) is exactly where the stamp should be."""
    if mode == "annotation":
        boxes = [list(a.rect) for a in page.annots() if a.type[1] == "Stamp"]
    else:
        boxes = [list(info["bbox"]) for info in page.get_image_info()]
    return any(all(abs(a - b) <= 1.5 for a, b in zip(box, want)) for box in boxes)


def date_check(page, date, date_text, mode, flattened):
    """Did the date land? Returns (title, passed, detail)."""
    if date["kind"] == "field" and not flattened:
        values = [w.field_value for w in page.widgets() if w.xref == date["field_xref"]]
        return "Date filled in the form field", values == [date_text], f"field {date.get('field_name')}"
    if mode == "annotation":
        texts = [a.info.get("content", "") for a in page.annots() if a.type[1] == "FreeText"]
        return "Date added", date_text in texts, date_text
    zone = pymupdf.Rect(date["rect"]) + (-3, -3, 3, 3)
    return "Date added", flattened or date_text in page.get_text("text", clip=zone), date_text


def nothing_else_changed(before, after, placements, flattened):
    """Every original word is still in place, and the pages without a stamp are identical."""
    stamped = {p["page"] - 1 for p in placements}
    for i in range(before.page_count):
        if not set(word_positions(before[i])) <= set(word_positions(after[i])):
            return False
        if i not in stamped and not flattened and page_snapshot(before[i]) != page_snapshot(after[i]):
            return False
    return True


def check_signed_copy(original, signed, placements, mode, date_text, flattened):
    """Code checks after stamping: every stamp in place, dates filled, nothing else changed, client signatures valid."""
    checks = []

    def add(title, passed, detail=""):
        checks.append({"title": title, "passed": bool(passed), "detail": detail})

    with pymupdf.open(original) as before, pymupdf.open(signed) as after:
        add("Same number of pages", before.page_count == after.page_count, f"{before.page_count} -> {after.page_count}")
        for placement in placements:
            page = after[placement["page"] - 1]
            want = placement["signature"]
            name = f"spot {placement['spot_id']}" if placement.get("spot_id") else "your placement"
            add(f"Signature stamp is in place ({name})", stamp_landed(page, want, mode),
                f"page {placement['page']}, x {want[0]}-{want[2]}, y {want[1]}-{want[3]}")
            if placement.get("date"):
                title, passed, detail = date_check(page, placement["date"], date_text, mode, flattened)
                add(f"{title} ({name})", passed, detail)
        add("Nothing else changed", nothing_else_changed(before, after, placements, flattened),
            "every original word is still in place; other pages are identical")
    if mode == "annotation":
        results = validate_signatures(signed)
        broken = [r["field"] for r in results if not (r["intact"] and r["valid"])]
        add("Client signatures still valid", results and not broken,
            f"{len(results)} signature(s) checked" + (f"; broken: {', '.join(broken)}" if broken else ""))
    return checks


def sign_pdf(original, out_path, placements, signature_png, date_text, mode, flatten, signer_name):
    """Stamp the signature and date at every placement on a copy. The original is never opened for writing.

    Unsigned PDFs get the image drawn into the page. PDFs the client already signed get image stamp
    annotations and an append-only save, so their signed bytes stay untouched. All placements go
    into one save, so the copy is either fully signed or not written at all.
    """
    out_path = Path(out_path)
    work = out_path.with_name(out_path.stem + ".work.pdf")          # the copy being stamped
    rewritten = work.with_name(work.stem + ".full.pdf")              # used when the copy is saved in full
    flattened = bool(flatten) and mode == "content"
    shutil.copy2(original, work)
    try:
        with pymupdf.open(work) as doc:
            for placement in placements:
                page = doc[placement["page"] - 1]
                stamp_signature(page, placement["signature"], signature_png, mode, signer_name)
                if placement.get("date"):
                    write_date(page, placement["date"], date_text, mode, signer_name)
            if flattened:
                doc.bake(annots=True, widgets=True)       # fields become plain text nobody can edit later
            append_only = doc.can_save_incrementally()
            if mode == "annotation" and not append_only:
                raise ValueError("This signed PDF can't be updated append-only, so the client's signature would break")
            save_in_full = mode == "content" and (flattened or not append_only)
            if save_in_full:
                doc.save(rewritten, garbage=3, deflate=True)
            else:
                doc.saveIncr()                             # append-only: the original bytes stay as they were
        if save_in_full:
            os.replace(rewritten, work)                    # only once the document is closed
    except Exception:
        work.unlink(missing_ok=True)
        rewritten.unlink(missing_ok=True)
        raise
    checks = check_signed_copy(original, work, placements, mode, date_text, flattened)
    ok = all(c["passed"] for c in checks)
    if ok:
        os.replace(work, out_path)                         # write under a temporary name, then rename
    else:
        work.unlink(missing_ok=True)
    return {"ok": ok, "checks": checks, "flattened": flattened}


# =============================================================================
# Pictures: page previews for the GUI, and cleaning the signature image
# =============================================================================

GREEN, RED, BLUE, GREY = (0, 0.55, 0.2), (0.75, 0.1, 0.1), (0.1, 0.35, 0.85), (0.45, 0.45, 0.45)
OWNER_COLORS = {"ours": GREEN, "theirs": RED, "never": RED}      # any other owner is grey


def outline(page, area, number, color, width):
    """A dashed box with its number in the top-right corner."""
    page.draw_rect(pymupdf.Rect(area), color=color, width=width, dashes="[2 2] 0")
    page.insert_text((area[2] - 12, area[1] + 8), str(number), fontsize=8, color=color)


def draw_proposed_stamp(page, placement):
    """A filled green box where the stamp would go, and a blue one for its date."""
    r = placement["signature"]
    page.draw_rect(pymupdf.Rect(r), color=GREEN, fill=(0.2, 0.8, 0.4), fill_opacity=0.3, width=1.2)
    page.insert_text((r[0] + 3, r[1] + 10), "signature", fontsize=8, color=(0, 0.4, 0.15))
    date = placement.get("date")
    if date and date.get("rect"):
        page.draw_rect(pymupdf.Rect(date["rect"]), color=BLUE, fill=(0.4, 0.6, 1), fill_opacity=0.3, width=1)


def render_page(pdf_path, page_number, analysis=None, placements=None, dpi=110):
    """A PNG of one page. With an analysis, the spots are outlined and the proposed stamps are drawn in."""
    with pymupdf.open(pdf_path) as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"Page {page_number} doesn't exist")
        page = doc[page_number - 1]
        if analysis:
            for s in analysis["spots"]:
                if s["page"] == page_number:
                    outline(page, s["area"], s["id"], OWNER_COLORS.get(s["owner"], GREY), width=0.8)
            for d in analysis["dates"]:
                if d["page"] == page_number:
                    outline(page, d["area"], d["id"], BLUE, width=0.6)
        for placement in placements or []:
            if placement["page"] == page_number:
                draw_proposed_stamp(page, placement)
        return page.get_pixmap(dpi=dpi).tobytes("png")


def prepare_signature(data):
    """Clean a signature image for stamping: make a white background transparent and crop to the ink.

    Runs in memory on every use, so the signature file itself is never changed.
    """
    image = Image.open(io.BytesIO(data)).convert("RGBA")
    if image.getchannel("A").getextrema()[0] == 255:
        # No transparency yet: near-white becomes transparent so the stamp doesn't hide the line,
        # and darker pixels become more and more opaque.
        gray = image.convert("L")
        image.putalpha(gray.point(lambda v: 0 if v > 225 else min(255, (225 - v) * 3)))
    box = image.getchannel("A").getbbox()
    if box is None:
        raise ValueError("The image is empty once the white background is removed")
    image = image.crop(box)
    if image.width > 1200:
        image = image.resize((1200, max(1, round(image.height * 1200 / image.width))))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def image_aspect(png_bytes):
    """(width / height, width, height) of an image."""
    image = Image.open(io.BytesIO(png_bytes))
    return image.width / image.height, image.width, image.height
