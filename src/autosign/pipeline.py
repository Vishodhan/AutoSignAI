"""
Reading a PDF and finding its signature spots: their blocks, dates and name lines, and exactly
where each signature goes (DocuSeal's fields are placed there).

    Parse    read_page(): the words, lines, form fields and drawings of one page
    Detect   analyze(): the signature spots, their blocks and dates, who owns each, where a stamp
             fits, and the red flags that need a person's eye
    Place    placements_for_spots(): the exact rectangles of the signatures and their dates

The rules judge every block from the side of one party, called "ours" (and "you" in the texts).
`settings` describes that party: its name and aliases, the roles it usually has, and the names of
the other parties. In AutoSign that party is the client (see workflow.client_settings).

Coordinates are PDF points (72 pt = 1 inch) from the top-left corner of the page, as PyMuPDF
reports them. A rectangle is a list [x0, y0, x1, y1]. Every function takes and returns plain
values (dicts, lists, strings, numbers), so each result can be stored as JSON.
"""

import re
import time
from pathlib import Path

import pymupdf
from pyhanko.pdf_utils.reader import PdfFileReader

from autosign.policy import (
    BLOCK_REACH, DATE_FONT_SIZES, DOC_TYPE_PATTERNS, DROP_RATIOS, ENTITY_WORDS, EXECUTION_PATTERNS, FIELD_WORDS,
    HEADER_REACH, INSTRUCTION_PATTERNS, KNOWN_ROLES, LEGAL_SUFFIXES, MAX_LABEL_WORDS, MIN_LINE_WIDTH,
    NAME_LABEL_WORDS, NEVER_WORDS, NOT_A_SPOT_WORDS, NOT_SIGNING_DATE_WORDS, ROW_TOLERANCE, RULES_VERSION,
    SEGMENT_GAP, SIGN_WORDS, SIGNATURE_HEIGHTS,
)

# =============================================================================
# Helpers
# =============================================================================

def norm(text):
    """Lowercase, straighten quotes, drop blank runs (____, .....), squeeze spaces."""
    text = text or ""
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u00a0", " ")
    text = re.sub(r"_+|\u2026+|\.{3,}", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def tokens(text):
    """The words of a text, lowercase, without punctuation."""
    return re.findall(r"[a-z0-9]+", norm(text))


def phrase_pattern(phrase):
    """A regex that finds a phrase whatever the case, punctuation or spacing ('ACME ANALYTICS, INC.')."""
    words = re.findall(r"[A-Za-z0-9]+", phrase or "")
    if not words:
        return None
    escaped = [re.escape(word) for word in words]
    return r"(?<![A-Za-z0-9])" + r"[\W_]*".join(escaped) + r"(?![A-Za-z0-9])"


def find_phrase(text, phrase):
    """The first match of the phrase in the text, or None."""
    pattern = phrase_pattern(phrase)
    if pattern is None:
        return None
    return re.search(pattern, text or "", flags=re.IGNORECASE)


def mentions_any(text, names):
    """True when the text contains one of the names."""
    for name in names:
        if find_phrase(text, name):
            return True
    return False


def contains_same(items, text):
    """True when one of the items is the same text as `text` (ignoring case and spacing)."""
    for item in items:
        if norm(item) == norm(text):
            return True
    return False


def our_names(settings):
    """The names of the party the rules judge for."""
    return [settings["company_name"]] + settings["company_aliases"]


def rect_of(item):
    return [item["x0"], item["y0"], item["x1"], item["y1"]]


def rounded(rect):
    return [round(value, 1) for value in rect]


def bounds(items):
    """The smallest box around some words, as {x0, y0, x1, y1}."""
    return {
        "x0": min(item["x0"] for item in items),
        "y0": min(item["y0"] for item in items),
        "x1": max(item["x1"] for item in items),
        "y1": max(item["y1"] for item in items),
    }


def center_x(item):
    return (item["x0"] + item["x1"]) / 2


def center_y(item):
    return (item["y0"] + item["y1"]) / 2


def intersects(a, b, pad=0.6):
    """True when two rectangles overlap by more than `pad` points (touching is allowed)."""
    return a[0] < b[2] - pad and b[0] < a[2] - pad and a[1] < b[3] - pad and b[1] < a[3] - pad


def overlap(a0, a1, b0, b1):
    """How long the ranges a0-a1 and b0-b1 overlap (0 when they don't)."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def overlap_area(a, b):
    return overlap(a[0], a[2], b[0], b[2]) * overlap(a[1], a[3], b[1], b[3])


def join_text(rows):
    return " ".join(row["text"] for row in rows)


def find_by_id(items, item_id):
    """The spot (or date) with this id, or None."""
    for item in items:
        if item["id"] == item_id:
            return item
    return None


# =============================================================================
# Parse: digital signatures already in the PDF
# =============================================================================

def read_signatures(pdf_path):
    """Digital signatures already in the file, with the certification lock level (DocMDP) of each."""
    found = []
    with open(pdf_path, "rb") as f:
        reader = PdfFileReader(f, strict=False)
        for signature in reader.embedded_signatures:
            level = signature.docmdp_level
            lock_level = None
            if level is not None:
                lock_level = int(level.value)
            found.append({"field": signature.field_name, "lock_level": lock_level})
    return found


def has_signature_fields(doc):
    """True when the PDF has signature form fields, or says it has (/SigFlags)."""
    if doc.get_sigflags() > 0:
        return True
    for page in doc:
        for widget in page.widgets():
            if widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE:
                return True
    return False


def signature_state(pdf_path, doc=None):
    """The digital signatures already in the PDF, and the lock level they set.

    Always read from the file, never only when /SigFlags is set: some PDFs leave it out. 'blocked'
    says why the PDF must not be signed. 'mode' is 'annotation' for PDFs that are (or may be)
    digitally signed and 'content' for the others; AutoSign refuses the digitally signed ones
    (workflow.detect), because DocuSeal removes a PDF's own signature fields.
    """
    try:
        signatures = read_signatures(pdf_path)
        unreadable = None
    except Exception as error:
        # pyHanko couldn't parse the file. That only matters when there is a signature to protect.
        signatures = []
        unreadable = None
        if doc is None:
            with pymupdf.open(pdf_path) as opened:
                fields_found = has_signature_fields(opened)
        else:
            fields_found = has_signature_fields(doc)
        if fields_found:
            unreadable = error

    lock_levels = []
    for signature in signatures:
        if signature["lock_level"]:
            lock_levels.append(signature["lock_level"])
    lock = min(lock_levels, default=None)

    blocked = None
    if unreadable is not None:
        blocked = (f"The PDF has digital signature fields that can't be read ({unreadable}), so signing could "
                   "break them. Save it as a new PDF without form fields (print it to PDF) and upload that.")
    elif lock == 1:
        blocked = "The PDF is certified with lock level 1: no changes are allowed. Upload a copy that isn't certified."
    elif lock == 2:
        blocked = ("The PDF is certified with lock level 2 (form filling and signing only): a signature image "
                   "would break its certification. Upload a copy that isn't certified.")

    mode = "content"
    if signatures or unreadable is not None:
        mode = "annotation"
    return {"signatures": signatures, "lock_level": lock, "blocked": blocked, "mode": mode}


# =============================================================================
# Parse: the words, lines, form fields and drawings of one page
# =============================================================================

FIELD_KINDS = {pymupdf.PDF_WIDGET_TYPE_SIGNATURE: "signature", pymupdf.PDF_WIDGET_TYPE_TEXT: "text"}


def read_words(page):
    """Words with boxes. Runs of ____ or ..... are kept apart as 'blanks' (they are lines, not text)."""
    words = []
    blanks = []
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
    fields = []
    for widget in page.widgets():
        value = ""
        if isinstance(widget.field_value, str):
            value = widget.field_value.strip()
        fields.append({
            "name": widget.field_name or "",
            "kind": FIELD_KINDS.get(widget.field_type, "other"),
            "value": value,
            "rect": list(widget.rect),
            "xref": widget.xref,
        })
    return fields


def is_inside(word, rect):
    """True when the centre of a word lies inside a rectangle."""
    return rect[0] <= center_x(word) <= rect[2] and rect[1] <= center_y(word) <= rect[3]


def tag_field_words(words, fields):
    """Mark the words that are form-field values, so they are never read as labels.

    PyMuPDF already returns the visible text of filled fields (and of a visible digital signature,
    "Digitally signed by..."). A text field's value is added here only when it shows no text.
    """
    for field in fields:
        if field["kind"] not in ("text", "signature"):
            continue
        rect = field["rect"]
        inside = []
        for word in words:
            if is_inside(word, rect):
                inside.append(word)
        for word in inside:
            word["field"] = field["xref"]
        if field["kind"] == "text" and field["value"] and not inside:
            words.append({"x0": rect[0], "y0": rect[1], "x1": rect[2], "y1": rect[3], "text": field["value"],
                          "field": field["xref"]})


def make_line(x0, x1, y, source):
    return {"x0": x0, "x1": x1, "y": y, "source": source}


def is_dark(fill):
    """A fill colour darker than mid-grey."""
    return 0.3 * fill[0] + 0.59 * fill[1] + 0.11 * fill[2] < 0.5


def read_drawings(page):
    """Horizontal rules, vertical rules, outlined boxes, curves (possible ink) and dark fills."""
    found = {"lines": [], "verticals": [], "boxes": [], "curves": [], "dark_fills": []}
    for path in page.get_drawings():
        fill = path.get("fill")
        if fill and is_dark(fill):
            found["dark_fills"].append(list(path["rect"]))
        for item in path["items"]:
            kind = item[0]
            if kind == "l":
                add_straight_line(found, item[1], item[2])
            elif kind == "re":
                add_rectangle(found, item[1], path.get("color") is not None)
            elif kind == "c":                       # a curve: maybe someone's ink
                found["curves"].append(list(path["rect"]))
    return found


def add_straight_line(found, p1, p2):
    """A horizontal line long enough to sign on, or a vertical rule (part of a table or a box)."""
    if abs(p1.y - p2.y) < 1 and abs(p1.x - p2.x) >= MIN_LINE_WIDTH:
        found["lines"].append(make_line(min(p1.x, p2.x), max(p1.x, p2.x), (p1.y + p2.y) / 2, "drawn"))
    elif abs(p1.x - p2.x) < 1 and abs(p1.y - p2.y) >= 6:
        found["verticals"].append([p1.x - 0.5, min(p1.y, p2.y), p1.x + 0.5, max(p1.y, p2.y)])


def add_rectangle(found, rect, outlined):
    """A thin rectangle is a rule (Word exports rules that way); a bigger outlined one is a box."""
    if rect.height <= 2.5 and rect.width >= MIN_LINE_WIDTH:
        found["lines"].append(make_line(rect.x0, rect.x1, (rect.y0 + rect.y1) / 2, "drawn"))
    elif rect.width <= 2.5 and rect.height >= 6:
        found["verticals"].append(list(rect))
    elif rect.width >= 60 and rect.height >= 18 and outlined:
        found["boxes"].append(list(rect))


def underline_y(y0, y1, is_dots):
    """Where the line of a typed blank is: underscores sit at the bottom of the box, dots a little higher."""
    if is_dots:
        return y1 - 0.2 * (y1 - y0)
    return y1 - 1


def text_lines(page, blanks):
    """Lines typed as text: runs of underscores or dots, including ones glued to a label ('By:____')."""
    found = []
    for blank in blanks:
        if blank["x1"] - blank["x0"] >= MIN_LINE_WIDTH:
            y = underline_y(blank["y0"], blank["y1"], blank["dots"])
            found.append(make_line(blank["x0"], blank["x1"], y, "text"))
    for run, is_dots in (("_____", False), (".....", True)):
        for rect in page.search_for(run):
            if rect.width >= MIN_LINE_WIDTH:
                found.append(make_line(rect.x0, rect.x1, underline_y(rect.y0, rect.y1, is_dots), "text"))
    return found


def same_line(a, b):
    """Two lines at the same height that touch or overlap."""
    return abs(a["y"] - b["y"]) <= 3 and b["x0"] <= a["x1"] + 3 and b["x1"] >= a["x0"] - 3


def merge_lines(lines):
    """One line per place: a drawn rule under a run of underscores is the same line."""
    merged = []
    for line in sorted(lines, key=lambda l: (l["y"], l["x0"])):
        match = None
        for kept in merged:
            if same_line(kept, line):
                match = kept
                break
        if match is None:
            merged.append(dict(line))
            continue
        match["x0"] = min(match["x0"], line["x0"])
        match["x1"] = max(match["x1"], line["x1"])
        if line["source"] == "drawn":
            match["y"] = line["y"]
            match["source"] = "drawn"
    return merged


def is_text_underline(line, words):
    """A rule that sits right under words is underlined text, not a signature line."""
    covered = 0
    for word in words:
        if word.get("field"):
            continue
        if word["y0"] - 1 <= line["y"] <= word["y1"] + 2.5:
            covered += overlap(word["x0"], word["x1"], line["x0"], line["x1"])
    return covered >= 0.5 * (line["x1"] - line["x0"])


def is_table_border(line, verticals):
    """A rule whose end touches a vertical rule is part of a table or box."""
    for v in verticals:
        if v[1] - 2 <= line["y"] <= v[3] + 2:
            for end in (line["x0"], line["x1"]):
                if v[0] - 3 <= end <= v[2] + 3:
                    return True
    return False


def is_white(color):
    """A 0xRRGGBB colour that is nearly white."""
    red = (color >> 16) & 255
    green = (color >> 8) & 255
    blue = color & 255
    return min(red, green, blue) >= 235


def is_on_dark_fill(x, y, dark_fills):
    for fill in dark_fills:
        if fill[0] <= x <= fill[2] and fill[1] <= y <= fill[3]:
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
    if is_white(span["color"]) and not is_on_dark_fill((x0 + x1) / 2, (y0 + y1) / 2, dark_fills):
        return "white"                      # white text, and not on a dark background
    return None


def hidden_text(page, dark_fills):
    """Text a reader can't see: invisible, white on white, tiny or off the page."""
    found = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"].strip()
                if len(text) < 2:
                    continue
                why = why_hidden(span, page, dark_fills)
                if why:
                    found.append({"text": text[:80], "why": why})
    return found


def unrotated_size(page):
    """Page width and height before rotation: PyMuPDF reports positions in the unrotated page."""
    width = page.rect.width
    height = page.rect.height
    if page.rotation in (90, 270):
        return height, width
    return width, height


def looks_scanned(words, images, width, height):
    """A scan: almost no text, and one image covering most of the page."""
    if len(words) >= 10:
        return False
    for image in images:
        if (image[2] - image[0]) * (image[3] - image[1]) > 0.6 * width * height:
            return True
    return False


def read_page(doc, index):
    """Everything the rules need about one page."""
    page = doc[index]
    words, blanks = read_words(page)
    fields = read_fields(page)
    tag_field_words(words, fields)
    drawings = read_drawings(page)
    lines = []
    for line in merge_lines(drawings["lines"] + text_lines(page, blanks)):
        if not is_text_underline(line, words) and not is_table_border(line, drawings["verticals"]):
            lines.append(line)
    images = [list(info["bbox"]) for info in page.get_image_info()]
    annots = [{"type": annot.type[1], "rect": list(annot.rect)} for annot in page.annots()]
    width, height = unrotated_size(page)
    return {
        "number": index + 1, "width": width, "height": height, "rotation": page.rotation,
        "words": words, "blanks": blanks, "lines": lines, "fields": fields, "boxes": drawings["boxes"],
        "images": images, "annots": annots, "curves": drawings["curves"],
        "hidden": hidden_text(page, drawings["dark_fills"]),
        "scanned": looks_scanned(words, images, width, height),
    }


def page_summary(page):
    """What the analysis keeps about a page."""
    return {"number": page["number"], "width": round(page["width"], 1), "height": round(page["height"], 1),
            "rotation": page["rotation"], "scanned": page["scanned"]}


# =============================================================================
# Detect: the structure of a page (rows, labels, headers and footers)
# =============================================================================

def group_rows(words):
    """Group words into visual rows by their bottom edge."""
    groups = []
    for word in sorted(words, key=lambda w: (w["y1"], w["x0"])):
        if groups and word["y1"] - groups[-1][0]["y1"] <= ROW_TOLERANCE:      # close to the row's first word
            groups[-1].append(word)
        else:
            groups.append([word])
    rows = []
    for group in groups:
        row_words = sorted(group, key=lambda w: w["x0"])
        rows.append({"words": row_words, **bounds(row_words), "text": join_text(row_words)})
    return rows


def mark_boilerplate(pages):
    """Text repeated at the same height on most pages is a header or footer. Rules ignore it."""
    if len(pages) < 3:
        return
    seen = {}       # the row's text, digits replaced by '#' -> [(page number, row), ...]
    for page in pages:
        for row in group_rows(page["words"]):
            key = re.sub(r"\d+", "#", norm(row["text"]))
            if len(key) >= 3:
                seen.setdefault(key, []).append((page["number"], row))
    needed = max(3, round(len(pages) * 0.6))
    for hits in seen.values():
        page_numbers = {number for number, _ in hits}
        heights = [row["y1"] for _, row in hits]
        if len(page_numbers) >= needed and max(heights) - min(heights) <= 24:
            for _, row in hits:
                for word in row["words"]:
                    word["boiler"] = True


def page_text(rows):
    return "\n".join(row["text"] for row in rows)


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


def split_row(words):
    """The groups of words in a row, split at wide gaps and wherever a form field starts or ends."""
    groups = []
    current = [words[0]]
    for word in words[1:]:
        previous = current[-1]
        if word["x0"] - previous["x1"] > SEGMENT_GAP or word.get("field") != previous.get("field"):
            groups.append(current)
            current = [word]
        else:
            current.append(word)
    groups.append(current)
    return groups


def make_segments(rows):
    """Split each row into separate labels where there is a wide gap ('Signature ____ Date ____').

    A form field's value is always a segment of its own, of kind 'value'.
    """
    segments = []
    for row in rows:
        for group in split_row(row["words"]):
            text = join_text(group)
            if group[0].get("field"):
                kind = "value"
            else:
                kind = classify(text)
            segments.append({**bounds(group), "text": text, "kind": kind})
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
    words = []
    for row in rows:
        for word in row["words"]:
            if col[0] <= center_x(word) <= col[1]:
                words.append(word)
    return group_rows(words)


def is_paragraph_tail(row, rows, page_width):
    """'Employee.' alone on a row is the end of a paragraph, not a header."""
    for other in rows:
        if row_kind(other, page_width) != "long":
            continue
        just_below = 0 <= row["y0"] - other["y1"] <= 6
        if just_below and overlap(other["x0"], other["x1"], row["x0"], row["x1"]) > 0:
            return True
    return False


# =============================================================================
# Detect: signature spots
# =============================================================================

SPOT_LABELS = ("sign", "by", "never")       # label kinds that mark a spot ('never': one nobody signs here)


def spot_kind(label):
    if label["kind"] == "never":
        return "never"
    return "signature"


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


def text_between(rows, seg, line):
    """True when a row is printed between a label and a line further down, across the line's width."""
    for row in rows:
        below_label = row["y0"] > seg["y1"] - 1
        above_line = row["y1"] < line["y"] - 1
        if below_label and above_line and overlap(row["x0"], row["x1"], line["x0"], line["x1"]) > 0:
            return True
    return False


def free_line_below(seg, lines, rows):
    """An unlabeled line within 60 pt under a 'Signature:' label, with nothing printed between."""
    best = None
    for line in lines:
        if line["role"] != "free":
            continue
        gap = line["y"] - seg["y1"]
        lined_up = line["x0"] <= seg["x0"] + 15 and line["x1"] >= seg["x0"] + 60
        if not (2 < gap <= 60 and lined_up):
            continue
        if text_between(rows, seg, line):
            continue
        if best is None or line["y"] < best["y"]:
            best = line
    return best


def space_right_is_free(seg, page, needed=130):
    """Nothing printed to the right of the label on its row for `needed` points."""
    zone = [seg["x1"] + 2, seg["y0"] - 4, seg["x1"] + needed, seg["y1"] + 2]
    if zone[2] > page["width"] - 18:
        return False
    for word in page["words"]:
        if intersects(zone, rect_of(word)):
            return False
    for field in page["fields"]:
        if intersects(zone, field["rect"]):
            return False
    return True


def line_coords(line):
    return [round(line["x0"], 1), round(line["y"], 1), round(line["x1"], 1)]


def vertical_extent(shape, label, line, area):
    """The top and bottom of a spot, its label included."""
    if shape == "label_then_line":
        return min(label["y0"], line["y"] - 12), max(label["y1"], line["y"] + 2)
    if shape == "line_then_label_below":
        return line["y"] - 12, label["y1"]
    if shape == "label_then_line_below":
        return label["y0"], line["y"] + 2
    if shape == "label_blank":
        return label["y0"] - 2, label["y1"] + 2
    # a signature field or a box
    top = area[1]
    bottom = area[3]
    if label:
        top = min(top, label["y0"])
        bottom = max(bottom, label["y1"])
    return top, bottom


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
    top, bottom = vertical_extent(shape, label, line, area)

    left = area[0]
    right = area[2]
    label_text = ""
    label_rect = None
    if label:
        left = min(left, label["x0"])
        right = max(right, label["x1"])
        label_text = label["text"]
        label_rect = rounded(rect_of(label))
    line_value = None
    if line:
        line_value = line_coords(line)
    return {
        "page": page["number"], "kind": kind, "shape": shape, "label": label_text, "label_rect": label_rect,
        "line": line_value, "area": rounded(area), "top": top, "bottom": bottom, "col": [left - 8, right + 8],
    }


def line_spots(page, segments, used):
    """Spots on lines, each labelled on its left ('Signature: ____') or under it.

    Also records what every line is for in line["role"]: 'spot', 'date', 'field' or 'free'.
    """
    lines = page["lines"]
    # Labels on the left first, so 'Name: ____' labels the name line and is never taken as the
    # label printed under the line above it.
    left_labels = set()
    for line in lines:
        line["left"] = label_left_of(line, segments)
        if line["left"]:
            left_labels.add(id(line["left"]))
    spots = []
    for line in lines:
        line["below"] = label_below(line, segments, left_labels)
        # The label on the left decides; if it is plain text, the label under the line does.
        if line_role(line["left"]):
            label = line["left"]
            shape = "label_then_line"
        else:
            label = line["below"]
            shape = "line_then_label_below"
        line["role"] = line_role(label) or "free"
        if line["role"] == "spot":
            spots.append(new_spot(page, shape, label, spot_kind(label), line=line))
            used.add(id(label))
    return spots


def nearest_label(rect, segments):
    """The label next to a form field: same row on the left, else just above or below."""
    best = None
    best_distance = float("inf")
    for seg in segments:
        if seg["kind"] not in ("sign", "by", "never", "text"):          # not a date label or a field value
            continue
        overlaps = overlap(seg["x0"], seg["x1"], rect[0], rect[2]) > 0
        same_row = abs(seg["y1"] - rect[3]) <= 8 and 0 <= rect[0] - seg["x1"] <= 80
        above = 0 <= rect[1] - seg["y1"] <= 20 and overlaps
        below = 0 <= seg["y0"] - rect[3] <= 14 and overlaps
        if not (same_row or above or below):
            continue
        distance = abs(seg["y1"] - rect[3]) + abs(seg["x0"] - rect[0]) * 0.1
        if distance < best_distance:
            best = seg
            best_distance = distance
    return best


def field_spots(page, segments, used):
    """Empty signature form fields (PDF fields made for signing), each with the label next to it."""
    spots = []
    for field in page["fields"]:
        if field["kind"] != "signature" or field.get("signed"):
            continue
        label = nearest_label(field["rect"], segments)
        spot = new_spot(page, "signature_field", label, "signature", rect=field["rect"])
        spot["field_xref"] = field["xref"]
        spots.append(spot)
        if label:
            used.add(id(label))
    return spots


def labels_box(seg, box):
    """True when the label is printed inside the box or just above it."""
    inside_x = box[0] - 1 <= seg["x0"] and seg["x1"] <= box[2] + 1
    inside_y = box[1] - 1 <= seg["y0"] and seg["y1"] <= box[3] + 1
    above = box[1] - 14 <= seg["y1"] <= box[1] + 2 and overlap(seg["x0"], seg["x1"], box[0], box[2]) > 0
    return (inside_x and inside_y) or above


def box_label(box, segments, used):
    """The first unused signature label inside or just above a box, or None."""
    for seg in segments:
        if seg["kind"] in ("sign", "by") and id(seg) not in used and labels_box(seg, box):
            return seg
    return None


def box_spots(page, segments, used):
    """Outlined boxes with a signature label inside or just above."""
    spots = []
    for box in page["boxes"]:
        label = box_label(box, segments, used)
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


def covers_a_kept_spot(spot, kept):
    """True when more than 30% of the spot's area is already taken by a spot that was kept."""
    a = spot["area"]
    own_area = max(1.0, (a[2] - a[0]) * (a[3] - a[1]))
    for other in kept:
        if other["page"] == spot["page"] and overlap_area(a, other["area"]) > 0.3 * own_area:
            return True
    return False


def drop_duplicate_spots(spots):
    """Keep one spot per place. A form field beats a line, a line a box, and a box blank space."""
    kept = []
    for spot in sorted(spots, key=lambda s: SHAPE_PRIORITY[s["shape"]]):
        if not covers_a_kept_spot(spot, kept):
            kept.append(spot)
    return sorted(kept, key=lambda s: (s["page"], s["top"], s["area"][0]))


def find_spots(page, rows):
    """Find every signature spot, 'Date' label and 'Name' label on a page. Returns (spots, date labels, name labels)."""
    segments = make_segments(rows)
    used = set()            # ids of the labels that already have a spot
    # The order matters: each kind of spot only takes labels the kinds before it left unused.
    spots = line_spots(page, segments, used)
    spots += field_spots(page, segments, used)
    spots += box_spots(page, segments, used)
    spots += lone_label_spots(page, segments, rows, used)
    date_segments = [seg for seg in segments if seg["kind"] == "date"]
    name_segments = [seg for seg in segments if seg["kind"] == "text" and is_name_label(seg["text"])]
    return drop_duplicate_spots(spots), date_segments, name_segments


# =============================================================================
# Detect: each spot's block (header, name, title) and its date
# =============================================================================

def closer_to_next(between, index, upper, lower):
    """True when a row sits at least as close to what is below it as to what is above it."""
    row = between[index]
    if index > 0:
        edge_above = between[index - 1]["y1"]
    else:
        edge_above = upper["bottom"]
    if index + 1 < len(between):
        edge_below = between[index + 1]["y0"]
    else:
        edge_below = lower["top"]
    return row["y0"] - edge_above + 2 >= edge_below - row["y1"]


def header_start(between, upper, lower, page_width):
    """Where the lower block's header starts among the rows between two stacked spots.

    The upper block's own field rows (Name, Title...) come first. Then the first row that sits
    closer to the lower block than to the row above it starts the lower block's header.
    """
    split = 0
    while split < len(between) and row_kind(between[split], page_width) in ("field", "entity", "blank"):
        split += 1
    while split < len(between):
        if row_kind(between[split], page_width) != "long" and closer_to_next(between, split, upper, lower):
            break
        split += 1
    return split


def split_between(rows, upper, lower, page_width):
    """Rows between two stacked spots: trailing fields of the upper block, then the lower block's header."""
    col = [min(upper["col"][0], lower["col"][0]), max(upper["col"][1], lower["col"][1])]
    between = []
    for row in column_rows(rows, col):
        if row["y0"] >= upper["bottom"] - 1 and row["y1"] <= lower["top"] + 1:
            between.append(row)
    split = header_start(between, upper, lower, page_width)
    upper_rows = []
    for row in between[:split]:
        if row_kind(row, page_width) != "long":
            upper_rows.append(row)
    lower_rows = []
    for row in between[split:]:
        if row_kind(row, page_width) != "long" and row["y0"] >= lower["top"] - HEADER_REACH:
            lower_rows.append(row)
    return upper_rows, lower_rows


def header_above(rows, spot, page_width):
    """Short rows just above a spot that has no spot above it ('PARTY A', 'ACME ANALYTICS INC.')."""
    candidates = []
    for row in column_rows(rows, spot["col"]):
        if spot["top"] - HEADER_REACH <= row["y0"] and row["y1"] <= spot["top"] + 1:
            candidates.append(row)
    result = []
    edge = spot["top"]
    for row in sorted(candidates, key=lambda r: -r["y1"]):          # from the spot upwards
        if row_kind(row, page_width) in ("long", "field"):
            break
        if result:
            largest_gap = 20
        else:
            largest_gap = 30
        if edge - row["y1"] > largest_gap:
            break
        if is_paragraph_tail(row, rows, page_width):
            break
        result.insert(0, row)
        edge = row["y0"]
    return result


def rows_below(rows, spot, page_width):
    """Name, Title and Date rows under a spot that has no spot below it."""
    candidates = []
    for row in column_rows(rows, spot["col"]):
        if spot["bottom"] - 1 <= row["y0"] <= spot["bottom"] + BLOCK_REACH:
            candidates.append(row)
    result = []
    edge = spot["bottom"]
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
    above = None
    below = None
    for other in spots:
        if other is spot:
            continue
        if overlap(other["col"][0], other["col"][1], spot["col"][0], spot["col"][1]) <= 0:
            continue
        if other["bottom"] <= spot["top"] + 2:
            if above is None or other["bottom"] > above["bottom"]:
                above = other
        if other["top"] >= spot["bottom"] - 2:
            if below is None or other["top"] < below["top"]:
                below = other
    return above, below


def tail_of_page(rows, page_width):
    """The last text rows at the bottom of a page (up to two)."""
    if not rows:
        return ""
    bottom = rows[-1]["y1"]
    tail = []
    for row in rows:
        if row["y0"] >= bottom - 40 and row_kind(row, page_width) == "text":
            tail.append(row)
    return join_text(tail[-2:])


def build_blocks(page, rows, spots, previous_rows):
    """Give each spot its block: the header above, its own label rows, and the field rows below.

    Sets 'header', 'own_text', 'below', 'block_text' and 'borrowed_header' on each spot, and keeps
    the block's rows in '_block_rows' for find_date_for.
    """
    width = page["width"]
    for spot in spots:
        upper, lower = stacked_neighbours(spot, spots)
        if upper:
            header = split_between(rows, upper, spot, width)[1]
        else:
            header = header_above(rows, spot, width)
        if lower:
            trailing = split_between(rows, spot, lower, width)[0]
        else:
            trailing = rows_below(rows, spot, width)
        own = []
        for row in column_rows(rows, spot["col"]):
            if row["y1"] > spot["top"] and row["y0"] < spot["bottom"]:
                own.append(row)

        spot["header"] = join_text(header)
        spot["own_text"] = join_text(own)
        spot["below"] = join_text(trailing)
        parts = []
        for part in (spot["header"], spot["own_text"], spot["below"]):
            if part:
                parts.append(part)
        spot["block_text"] = " ".join(parts).strip()
        spot["_block_rows"] = header + own + trailing
        # A spot at the top of the page with nothing above it may belong to a block split over the
        # page break, its header at the bottom of the previous page. That text is kept apart: it
        # can also be a notice address, so it is weak evidence (see judge_spot).
        spot["borrowed_header"] = ""
        if not header and not upper and spot["top"] < 140:
            spot["borrowed_header"] = tail_of_page(previous_rows, width)


def starts_right_after(seg, rect):
    """A form field that starts just right of a label, on its row."""
    after_the_label = seg["x1"] - 6 <= rect[0] <= seg["x1"] + 60
    on_its_row = rect[1] < seg["y1"] + 3 and rect[3] > seg["y0"] - 14
    return after_the_label and on_its_row


def field_target(field):
    return {"kind": "field", "area": rounded(field["rect"]), "field_name": field["name"],
            "field_xref": field["xref"], "value": field["value"]}


def field_on_line(page, line):
    """A text form field drawn on a line (its bottom edge on the line, over most of its length), or None."""
    line_length = line["x1"] - line["x0"]
    for field in page["fields"]:
        if field["kind"] != "text":
            continue
        x0, _top, x1, bottom = field["rect"]
        on_the_line = abs(bottom - line["y"]) <= 4
        overlap = min(x1, line["x1"]) - max(x0, line["x0"])
        if on_the_line and overlap >= line_length / 2:
            return field
    return None


def label_target(seg, page):
    """What to fill for a 'Date' or 'Name' label: a form field next to it, its line (or the form field
    on that line), or blank space after it."""
    for field in page["fields"]:
        if field["kind"] == "text" and starts_right_after(seg, field["rect"]):
            return field_target(field)
    for line in page["lines"]:
        if line.get("left") is seg or line.get("below") is seg:
            field = field_on_line(page, line)
            if field is not None:
                return field_target(field)
            area = [line["x0"], line["y"] - 12, line["x1"], line["y"] + 2]
            return {"kind": "line", "area": rounded(area), "line": line_coords(line)}
    area = [seg["x1"] + 4, seg["y0"], seg["x1"] + 84, seg["y1"]]
    return {"kind": "blank", "area": rounded(area), "label_rect": rounded(rect_of(seg))}


def in_block_below(seg, spot):
    """A label on one of the rows of the spot's block, below the spot."""
    if seg["y0"] < spot["bottom"] - 2:
        return False
    for row in spot["_block_rows"]:
        if abs(seg["y1"] - row["y1"]) <= 2 and seg["x0"] >= row["x0"] - 2 and seg["x1"] <= row["x1"] + 2:
            return True
    return False


def signature_row(spot):
    """Where a spot's signature sits: (row_y, left_edge, right_edge) of its line, or of its area."""
    if spot["line"]:
        return spot["line"][1], spot["line"][0], spot["line"][2]
    return spot["area"][3] - 4, spot["area"][0], spot["area"][2]


def labels_near(spot, segments):
    """(distance, label) for every label on the spot's row to its right, or in its block below.

    Distances are measured from the left edge of the signature, where a reader starts. Blocks side by
    side share their text rows, so both columns' labels are in each block; measured from the left,
    each signature keeps the label of its own column.
    """
    row_y, left_edge, right_edge = signature_row(spot)
    candidates = []
    for seg in segments:
        same_row = abs(seg["y1"] - row_y) <= 6 and right_edge - 5 <= seg["x0"] <= right_edge + 250
        if same_row or in_block_below(seg, spot):
            distance = abs(seg["y1"] - row_y) + abs(seg["x0"] - left_edge) * 0.2
            candidates.append((distance, seg))
    return candidates


def located(seg, page):
    """What to fill for a label, and where the label is."""
    target = label_target(seg, page)
    target.update(page=page["number"], label=seg["text"], label_rect=rounded(rect_of(seg)))
    return target


def nearest(candidates):
    return min(candidates, key=lambda candidate: candidate[0])[1]


def find_date_for(spot, page, date_segments):
    """The date that belongs to a signature spot: on its row to the right, or in its block below."""
    candidates = labels_near(spot, date_segments)
    if spot["line"]:
        row_y, left_edge, right_edge = signature_row(spot)
        # A date line on the same row whose 'Date' label is printed under it.
        for line in page["lines"]:
            if line["role"] != "date" or not line.get("below"):
                continue
            if abs(line["y"] - row_y) <= 4 and right_edge - 5 <= line["x0"] <= right_edge + 250:
                candidates.append((abs(line["x0"] - left_edge) * 0.2, line["below"]))
    if not candidates:
        return None
    return located(nearest(candidates), page)


def is_name_label(text):
    """'Name:', 'Print Name', 'Printed name', 'Name of signatory': where the signer's printed name goes."""
    toks = tokens(text)
    if "name" not in toks or len(toks) > 4:
        return False
    for tok in toks:
        if tok not in NAME_LABEL_WORDS:
            return False
    return True


def nothing_written(target, page):
    """Nothing is written yet where a name would go: no form field value, and no word on its line."""
    if target["kind"] == "field":
        return not str(target.get("value") or "").strip()
    x0, y0, x1, y1 = target["area"]
    for word in page["words"]:
        inside = word["x0"] >= x0 - 1 and word["x1"] <= x1 + 1 and word["y0"] >= y0 - 1 and word["y1"] <= y1 + 2
        if inside:
            return False
    return True


def find_name_for(spot, page, name_segments):
    """Where the signer's printed name goes in a spot's block ('Name: ____', or 'Print Name' under a
    line), or None. A name line that already has something on it is left alone."""
    candidates = labels_near(spot, name_segments)
    if not candidates:
        return None
    target = located(nearest(candidates), page)
    if not nothing_written(target, page):
        return None
    return target


# =============================================================================
# Detect: who owns each block
# =============================================================================

def ownership(owner, evidence, reason, role=None):
    """A judgement of who owns a block, in the keys a spot stores it under.

    owner is 'ours', 'theirs', 'never' or 'unknown'. evidence says how sure: 'name' (our name or
    signer is in the block), 'role' (our role, defined next to our name), 'unnamed_party' (the only
    party the contract leaves unnamed), 'usual_role' (a role we usually have), 'empty_block' (still
    empty after the other party signed) or 'page_break' (named only at the bottom of the previous page).
    """
    return {"owner": owner, "evidence": evidence, "owner_reason": reason, "role": role}


def names_to_look_for(parties, settings):
    """(owner, whose name, name) for every name that tells who owns a block, ours first."""
    names = []
    for name in our_names(settings) + parties["extra_aliases"]:
        names.append(("ours", "your company's name", name))
    names.append(("ours", "your signer's name", settings["signer_name"]))
    for name in parties["other_names"]:
        names.append(("theirs", "the other party's name", name))
    return names


def judge_owner(spot, parties, settings):
    """Who owns a block, judged on its own text: by the names in it first, then by the roles."""
    text = spot["block_text"] + " " + spot["label"]
    if spot["kind"] == "never" or NEVER_WORDS & set(tokens(text)):
        return ownership("never", None, "the block is for a witness, notary or guarantor")
    for owner, whose, name in names_to_look_for(parties, settings):
        hit = find_phrase(text, name)
        if hit:
            return ownership(owner, "name", f"the block shows {whose} '{hit.group(0)}'")
    roles = roles_in_text(text, parties["roles"] + settings["usual_roles"])
    if parties["our_role"]:
        return judge_by_contract_role(roles, parties)
    return judge_by_usual_role(roles, parties, settings)


def judge_by_contract_role(roles, parties):
    """Judge a block by the roles it names, when the contract says which role is ours."""
    our_role = parties["our_role"]
    mine = [role for role in roles if norm(role) == norm(our_role)]
    others = [role for role in roles if norm(role) != norm(our_role)]
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
    """Judge a block by the roles it names, when the contract doesn't say which role is ours.

    One of our usual roles counts as ours, unless the contract gives it to a named other party.
    """
    usual = [norm(role) for role in settings["usual_roles"]]
    taken = [norm(role) for role in parties["named_roles"]]
    mine = []
    others = []
    for role in roles:
        if norm(role) in usual and norm(role) not in taken:
            mine.append(role)
        else:
            others.append(role)
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

    1. Our usual roles must point to one role only. If one block says 'Disclosing Party' and
       another 'Receiving Party' and both are roles we usually have, nobody can tell which side
       we are, so none of them counts as ours.
    2. The empty blocks left: when the other party already signed some blocks, the blocks that are
       still empty and name no party are ours, but only if there are exactly as many of them as
       blocks the other party signed. A block the other party forgot to sign is then never
       mistaken for ours, which matters when a contract has several blocks per party.
    """
    usual = [spot for spot in spots if spot["evidence"] == "usual_role"]
    usual_roles = sorted({norm(spot["role"]) for spot in usual})
    if len(usual_roles) > 1:
        for spot in usual:
            spot.update(ownership("unknown", None, f"different blocks name different roles you usually have "
                                  f"({', '.join(usual_roles)}), so it isn't clear which side you are"))

    signature_spots = [spot for spot in spots if spot["kind"] == "signature"]
    signed = []
    empty_unknown = []
    already_ours = []
    for spot in signature_spots:
        if not spot["empty"] and spot["owner"] != "ours":
            signed.append(spot)
        if spot["empty"] and spot["owner"] == "unknown":
            empty_unknown.append(spot)
        if spot["owner"] == "ours":
            already_ours.append(spot)
    # Our blocks (already found plus the empty unnamed ones) must match the blocks the other party signed.
    if signed and empty_unknown and len(already_ours) + len(empty_unknown) == len(signed):
        reason = (f"the other party already signed {len(signed)} block(s); this is one of the "
                  f"{len(empty_unknown)} block(s) still empty, and it names no party")
        for spot in empty_unknown:
            spot.update(ownership("ours", "empty_block", reason))


def roles_in_text(text, roles):
    """Roles that appear in a text as whole words. Longer roles first, so 'Service Provider' isn't also 'Provider'."""
    unique = set()
    for role in roles:
        if role:
            unique.add(role)
    found = []
    remaining = text
    for role in sorted(unique, key=lambda r: (-len(r), r)):
        pattern = phrase_pattern(role)
        if pattern and re.search(pattern, remaining, flags=re.IGNORECASE):
            found.append(role)
            remaining = re.sub(pattern, " ", remaining, flags=re.IGNORECASE)
    return found


# =============================================================================
# Detect: the document type and the parties (from the opening pages)
# =============================================================================

def rules_doc_type(first_page_text):
    """The document type whose pattern appears first on page 1 (the title comes first)."""
    best_type = "other"
    best_position = None
    for doc_type, pattern in DOC_TYPE_PATTERNS:
        match = re.search(pattern, first_page_text, flags=re.IGNORECASE)
        if match and (best_position is None or match.start() < best_position):
            best_type = doc_type
            best_position = match.start()
    return best_type


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
    match = re.match(r"([^()]{0,160}?)\(([^()]{0,200})\)", text)
    if not match or re.search(r"\bbetween\b|\band\b|;", match.group(1), flags=re.IGNORECASE):
        return None
    return match.group(2)


def role_after(text, known):
    """The quoted role in the bracket right after a name."""
    for term in quoted_terms(bracket_after(text) or ""):
        if norm(term) in known:
            return term
    return None


def role_before(text, known):
    """A role label just before a name ('Service Provider: Acme Inc.')."""
    match = re.search(r"([A-Za-z][A-Za-z ]{1,30}?)\s*(?:#?\s*\d+)?\s*:\s*$", text)
    if match and norm(match.group(1)) in known:
        return match.group(1).strip()
    return None


def is_short_name(term, legal_name):
    """'Acme', 'Acme Analytics' and 'AA' are short names of 'Acme Analytics Inc.'; 'Party', 'Foo' and 'Inc' are not."""
    words = re.findall(r"[a-z0-9]+", legal_name.lower())
    core = [word for word in words if word not in LEGAL_SUFFIXES]
    if not core:
        core = words
    term_words = re.findall(r"[a-z0-9]+", term.lower())
    if not term_words:
        return False
    all_in_name = all(word in words for word in term_words)
    one_in_core = any(word in core for word in term_words)
    if all_in_name and one_in_core:
        return True
    joined = "".join(term_words)
    core_initials = "".join(word[0] for word in core)
    all_initials = "".join(word[0] for word in words)
    return len(core_initials) >= 2 and joined in (core_initials, all_initials)


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
    if len(re.findall(r"[A-Za-z]", text)) < 2:
        return ""
    return text


def defined_roles(opening, opening_rows, known):
    """The party roles the opening pages define: quoted in brackets ('(the "Client")') or as row labels ('Client:')."""
    terms = []
    for match in re.finditer(r"\(([^()]{0,200})\)", opening):
        terms.extend(quoted_terms(match.group(1)))
    for row in opening_rows:
        match = re.match(r"\s*([A-Za-z][A-Za-z ]{1,30}?)\s*(?:#?\s*\d+)?\s*:", row["text"])
        if match:
            terms.append(match.group(1))
    roles = []
    for term in terms:
        if norm(term) in known and not contains_same(roles, term):
            roles.append(term.strip())
    return roles


def role_near(before, after, known):
    """The role a label just before a name gives, else the bracket right after it. Returns (role, where)."""
    role = role_before(before, known)
    if role:
        return role, "label before your name"
    role = role_after(after, known)
    if role:
        return role, "definition right after your name"
    return None, None


def add_short_names(aliases, after, legal_name, known):
    """Add the short name the contract defines right after a full name, like Acme Analytics Inc. ("Acme")."""
    for term in quoted_terms(bracket_after(after) or ""):
        if norm(term) in known:
            continue
        if is_short_name(term, legal_name) and term not in aliases:
            aliases.append(term)


def find_party(opening, full_text, names, known):
    """Whether a party is named, the role the contract gives it, and the short names it defines for it.

    `names` are the party's names, its full name first. Every mention in the opening pages is
    looked at. A role counts only from a label just before a mention ('Service Provider: Acme Inc.')
    or the bracket right after it. If two mentions define different roles, nobody can tell which is
    right, so no role is taken.
    """
    party = {"found": False, "text": None, "role": None, "role_source": None, "aliases": []}
    found_roles = {}            # norm(role) -> (role, where it was found)
    for name in names:
        pattern = phrase_pattern(name)
        if pattern is None:
            continue
        for match in re.finditer(pattern, opening, flags=re.IGNORECASE):
            if not party["found"]:
                party["found"] = True
                party["text"] = match.group(0)
            before = opening[max(0, match.start() - 60):match.start()]
            after = opening[match.end():]
            role, source = role_near(before, after, known)
            if role:
                found_roles.setdefault(norm(role), (role, source))
            add_short_names(party["aliases"], after, names[0], known)

    if len(found_roles) == 1:
        party["role"], party["role_source"] = next(iter(found_roles.values()))
    elif len(found_roles) > 1:
        roles = ", ".join(role for role, _ in found_roles.values())
        party["role_source"] = "the contract gives your company different roles: " + roles

    if not party["found"]:            # not in the opening pages, but a mention anywhere still counts
        for name in names:
            match = find_phrase(full_text, name)
            if match:
                party["found"] = True
                party["text"] = match.group(0)
                break
    return party


def name_for_role(role, opening):
    """The name the opening gives a role: 'Client: Foo LLC', or 'Foo LLC (the "Client")'. Empty when blank."""
    after_label = phrase_pattern(role) + r"\s*(?:#?\s*\d+)?\s*:\s*([^\n]{2,80})"
    match = re.search(after_label, opening, flags=re.IGNORECASE)
    if match:
        name = clean_name(match.group(1))
        if name:
            return name
    before_bracket = r"([^\n:]{2,90})\(\s*(?:the\s+|hereinafter\s+)?[\u201c\"\u2018]" + re.escape(role)
    match = re.search(before_bracket, opening, flags=re.IGNORECASE)
    if match:
        last_part = re.split(r"\bbetween\b|\band\b|\bby\b", match.group(1))[-1]
        return clean_name(last_part)
    return ""


def name_other_parties(roles, our_role, opening, settings):
    """The other parties' names, from 'Client: Foo LLC' or 'Foo LLC (the "Client")'. Returns (names, their roles)."""
    names = []
    named_roles = []
    for role in roles:
        if our_role and norm(role) == norm(our_role):
            continue
        name = name_for_role(role, opening)
        if name and not mentions_any(name, our_names(settings)):
            names.append(name)
            named_roles.append(role)
    return names, named_roles


def add_known_other_parties(settings, opening, full_text, known, our_role, other_names, named_roles):
    """Add the parties the caller already knows are not ours (in AutoSign: the company sending the agreement).

    Their name, the short names the contract defines for them, and their role count as the other side.
    """
    for name in settings.get("other_party_names") or []:
        party = find_party(opening, full_text, [name], known)
        for found in [name, party["text"]] + party["aliases"]:
            if found and not contains_same(other_names, found):
                other_names.append(found)
        role = party["role"]
        if role and not contains_same(named_roles, role) and norm(role) != norm(our_role or ""):
            named_roles.append(role)


def find_parties(all_rows, settings):
    """Parties, their roles, and which one is ours. Uses pages 1-2 plus form-field values."""
    opening_rows = []
    for rows in all_rows[:2]:
        opening_rows.extend(rows)
    opening = page_text(opening_rows)
    full_text = "\n".join(page_text(rows) for rows in all_rows)
    known = set()
    for role in KNOWN_ROLES + settings["usual_roles"]:
        known.add(norm(role))

    roles = defined_roles(opening, opening_rows, known)
    our = find_party(opening, full_text, our_names(settings), known)
    other_names, named_roles = name_other_parties(roles, our["role"], opening, settings)
    add_known_other_parties(settings, opening, full_text, known, our["role"], other_names, named_roles)

    # The unnamed party: the contract names the other side ("Client: Foo LLC") and leaves exactly
    # one party's name blank. That blank party is ours. It works for any number of blocks, because
    # it identifies our role, and every block with that role is then ours.
    if not our["found"] and named_roles:
        blank = []
        for role in roles:
            if not contains_same(named_roles, role):
                blank.append(role)
        if len(blank) == 1:
            our["role"] = blank[0]
            our["role_source"] = "the only party whose name is left blank"
    return {
        "roles": roles, "our_name_found": our["found"], "our_name_text": our["text"],
        "our_role": our["role"], "our_role_source": our["role_source"],
        "extra_aliases": our["aliases"], "other_names": other_names, "named_roles": named_roles,
        "opening_text": opening[:2500],
    }


# =============================================================================
# Detect: where the stamp and the date go
# =============================================================================

def is_line_at(line, y, x0, x1, tolerance):
    """True when `line` ([x0, y, x1]) is at height y (within the tolerance) and overlaps x0-x1."""
    if not line:
        return False
    return abs(y - line[1]) <= tolerance and overlap(x0, x1, line[0], line[2]) > 0


def obstacles_for(page, skip_line=None, skip_field_xref=None):
    """Everything a stamp must not cover: words, form fields, other lines and blanks.

    `skip_line` ([x0, y, x1]) is the line the stamp sits on; `skip_field_xref` the form field it fills.
    """
    found = []
    for word in page["words"]:
        found.append(rect_of(word))
    for field in page["fields"]:
        if field["xref"] != skip_field_xref:
            found.append(field["rect"])
    for line in page["lines"]:
        if not is_line_at(skip_line, line["y"], line["x0"], line["x1"], 3):
            found.append([line["x0"], line["y"] - 0.5, line["x1"], line["y"] + 0.5])
    for blank in page["blanks"]:
        if not is_line_at(skip_line, blank["y1"] - 1, blank["x0"], blank["x1"], 4):
            found.append([blank["x0"], blank["y1"] - 2, blank["x1"], blank["y1"]])
    return found


def is_free(rect, obstacles, page):
    """Inside the page (with a 2 pt margin) and touching no obstacle."""
    if rect[0] < 2 or rect[1] < 2 or rect[2] > page["width"] - 2 or rect[3] > page["height"] - 2:
        return False
    for obstacle in obstacles:
        if intersects(rect, obstacle):
            return False
    return True


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
        area = spot["area"]
        room = area[3] - area[1] - 4
        heights = [height for height in SIGNATURE_HEIGHTS if height <= room]
        if not heights:
            heights = [room]
        attempts.append((area[3] - 2, area[0] + 2, area[2] - 2, heights, [0.0]))
    else:  # label_blank: right of the label on its row, else under the label
        label = spot["label_rect"]
        label_baseline = label[3] - 0.2 * (label[3] - label[1])
        right_limit = min(label[2] + 230, page["width"] - 20)
        attempts.append((label_baseline, label[2] + 6, right_limit, SIGNATURE_HEIGHTS, DROP_RATIOS))
        below_limit = min(label[0] + 230, page["width"] - 20)
        attempts.append((label[3] + 4 + SIGNATURE_HEIGHTS[0], label[0], below_limit, SIGNATURE_HEIGHTS, [0.0]))
    for attempt in attempts:
        found = first_free_rect(*attempt, aspect, obstacles, page)
        if found:
            rect, height = found
            return {"fits": True, "rect": rounded(rect), "height": height}
    return {"fits": False, "reason": "the signature doesn't fit there without touching text, fields or other lines"}


def spot_is_empty(page, spot, rect):
    """Nothing is already signed or drawn where the stamp would go. Returns (empty, why not)."""
    zone = rect or spot["area"]
    for image in page["images"]:
        if intersects(zone, image, pad=1):
            return False, "an image is already there"
    for curve in page["curves"]:
        if intersects(zone, curve, pad=1):
            return False, "a drawing (possibly a signature) is already there"
    for annot in page["annots"]:
        if annot["type"] in ("Ink", "Stamp", "FreeText") and intersects(zone, annot["rect"], pad=1):
            return False, f"a {annot['type']} annotation is already there"
    for field in page["fields"]:
        if field["kind"] == "signature" and field.get("signed") and intersects(zone, field["rect"], pad=1):
            return False, "a digital signature is already there"
    return True, ""


def place_date(page, date, date_text):
    """Where the date text goes. A form field gets a value; a line or blank gets text, at the largest
    size of DATE_FONT_SIZES that fits."""
    if date["kind"] == "field":
        if date.get("value"):
            return {"fits": False, "kind": "field", "reason": f"the date field already contains '{date['value']}'"}
        return {"fits": True, "kind": "field", "rect": date["area"], "field_xref": date["field_xref"],
                "field_name": date["field_name"]}
    if date["kind"] == "line":
        x0, y, x1 = date["line"]
        x = x0 + 3
        baseline = y - 2
        limit = x1 + 2              # the date stays on its line
        obstacles = obstacles_for(page, skip_line=date["line"])
    else:  # blank space after the label
        label = date["label_rect"]
        x = label[2] + 6
        baseline = label[3] - 0.22 * (label[3] - label[1])
        limit = page["width"]       # no line: only the page edge (and what is written there) limits it
        obstacles = obstacles_for(page)
    for size in DATE_FONT_SIZES:
        width = pymupdf.get_text_length(date_text, fontname="helv", fontsize=size)
        rect = [x, baseline - size * 0.8, x + width, baseline + size * 0.22]
        if x + width <= limit and is_free(rect, obstacles, page):
            return {"fits": True, "kind": "text", "point": [round(x, 1), round(baseline, 1)], "rect": rounded(rect),
                    "size": size}
    return {"fits": False, "kind": "text", "reason": "the date doesn't fit next to the Date label"}


# =============================================================================
# Detect: the whole document
# =============================================================================

def active_content(doc):
    """JavaScript, embedded files and XFA forms: unusual in a contract, so a person should look."""
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
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def all_words_text(pages):
    """The words of every page, one page per line."""
    page_lines = []
    for page in pages:
        page_lines.append(join_text(page["words"]))
    return "\n".join(page_lines)


def red_flags(pages, active, no_changes_allowed):
    """Reasons a person must look: hidden or instruction-like text, special signing rules, active content."""
    flags = []
    hidden = []
    for page in pages:
        hidden.extend(page["hidden"])
    if hidden:
        sample = "; ".join(f"'{item['text']}' ({item['why']})" for item in hidden[:3])
        flags.append(f"Hidden text found ({len(hidden)}): {sample}")
    text = norm(all_words_text(pages))
    injection = first_match(INSTRUCTION_PATTERNS, text)
    if injection is not None:
        flags.append(f"Text that reads like instructions to an AI: '{injection}'")
    clause = first_match(EXECUTION_PATTERNS, text)
    if clause is not None:
        flags.append(f"The contract sets a special way to sign: '{clause}'")
    for item in active:
        flags.append(f"The PDF contains {item}")
    if no_changes_allowed:
        flags.append("The PDF's permissions don't allow changes")
    return flags


def document_notes(pages, spots):
    """Things AutoSign leaves alone on purpose, reported so nobody is surprised."""
    notes = []
    initial_pages = 0
    for page in pages:
        for word in page["words"]:
            if "initial" in word["text"].lower():
                initial_pages += 1
                break
    if initial_pages:
        notes.append(f"Initials are requested on {initial_pages} page(s); they are left blank")
    for spot in spots:
        if spot["owner"] == "ours" and re.search(r"\btitle\b", norm(spot["below"])):
            notes.append(f"Spot {spot['id']}: the title line is left blank")
    return notes


def number_names(spots, spot_names, first_id):
    """Number the name lines found for the spots, from first_id. A line two spots both found is kept
    for both: workflow.overlap_problems then asks the company to check the places."""
    names = []
    for spot, name in zip(spots, spot_names):
        spot["name_id"] = None
        if name is None:
            continue
        name["id"] = first_id + len(names)
        name["spot_id"] = spot["id"]
        names.append(name)
        spot["name_id"] = name["id"]
    return names


def detect_spots(pages, all_rows, date_text):
    """Every spot of the document with its block, numbered in reading order; then their dates, then
    their name lines, numbered after them.

    Returns (spots, dates, names). Each spot's 'date_id' and 'name_id' point at its date and at the
    line for the signer's printed name (or are None).
    """
    spots = []
    spot_dates = []         # the date found for each spot, or None
    spot_names = []         # the name line found for each spot, or None
    for index, page in enumerate(pages):
        rows = all_rows[index]
        previous_rows = []
        if index > 0:
            previous_rows = all_rows[index - 1]
        page_spots, date_segments, name_segments = find_spots(page, rows)
        build_blocks(page, rows, page_spots, previous_rows)
        for spot in page_spots:
            date = None
            name = None
            if spot["kind"] == "signature":
                date = find_date_for(spot, page, date_segments)
                name = find_name_for(spot, page, name_segments)
            del spot["_block_rows"]
            spots.append(spot)
            spot_dates.append(date)
            spot_names.append(name)

    for number, spot in enumerate(spots, start=1):
        spot["id"] = number
    dates = []
    for spot, date in zip(spots, spot_dates):
        spot["date_id"] = None
        if date is None:
            continue
        date["id"] = len(spots) + len(dates) + 1
        date["spot_id"] = spot["id"]
        date["placement"] = place_date(pages[date["page"] - 1], date, date_text)
        dates.append(date)
        spot["date_id"] = date["id"]
    names = number_names(spots, spot_names, len(spots) + len(dates) + 1)
    return spots, dates, names


def judge_spot(spot, page, parties, settings, aspect):
    """Decide who owns a spot, where its stamp would go, and whether the spot is still empty."""
    spot.update(judge_owner(spot, parties, settings))
    if spot["owner"] == "unknown" and spot["borrowed_header"]:
        # Only the text at the bottom of the previous page names a party. That can be the block's
        # header, or a notice address: good enough to propose, never enough on its own.
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


def check_can_be_opened(doc):
    """Raises ValueError when the file isn't a PDF AutoSign can read."""
    if not doc.is_pdf:
        raise ValueError("The file is not a PDF")
    if doc.needs_pass:
        raise ValueError("The PDF is password-protected")
    if doc.page_count == 0:
        raise ValueError("The PDF has no pages")


def read_pages(doc, signatures):
    """Every page, read. The signature fields that hold one of the digital signatures are marked 'signed'."""
    signed_fields = {signature["field"] for signature in signatures}
    pages = []
    for index in range(doc.page_count):
        page = read_page(doc, index)
        for field in page["fields"]:
            field["signed"] = field["kind"] == "signature" and field["name"] in signed_fields
        pages.append(page)
    return pages


def analyze(pdf_path, settings, aspect):
    """Parse and Detect: every signature spot, who owns it, where its stamp and date go, and the red flags.

    `aspect` is the width / height of the signature stamps. Raises ValueError, with a reason a
    person can act on, when the file can't be signed at all.
    """
    started = time.time()
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as error:
        raise ValueError(f"The file can't be opened as a PDF ({error})")
    with doc:
        check_can_be_opened(doc)
        # Digital signatures already in the PDF are read first: they decide how it may be signed.
        state = signature_state(pdf_path, doc)
        if state["blocked"]:
            raise ValueError(state["blocked"])

        # Parse
        pages = read_pages(doc, state["signatures"])
        scanned = [page["number"] for page in pages if page["scanned"]]
        if len(scanned) > len(pages) / 2:
            raise ValueError(f"Scanned PDF: {len(scanned)} of {len(pages)} pages have no text layer. "
                             "Scans are not supported.")

        # Detect
        mark_boilerplate(pages)
        all_rows = []
        for page in pages:
            words = [word for word in page["words"] if not word.get("boiler")]
            all_rows.append(group_rows(words))
        parties = find_parties(all_rows, settings)
        spots, dates, names = detect_spots(pages, all_rows, time.strftime(settings["date_format"]))
        for spot in spots:
            judge_spot(spot, pages[spot["page"] - 1], parties, settings, aspect)
        resolve_owners(spots)
        no_changes_allowed = not doc.permissions & (pymupdf.PDF_PERM_MODIFY | pymupdf.PDF_PERM_ANNOTATE)
        flags = red_flags(pages, active_content(doc), no_changes_allowed)

    notes = []
    if scanned:
        notes.append(f"Pages without a text layer (scanned): {', '.join(str(number) for number in scanned)}")
    notes.extend(document_notes(pages, spots))
    return {
        "rules_version": RULES_VERSION,
        "page_count": len(pages),
        "pages": [page_summary(page) for page in pages],
        "doc_type": rules_doc_type(page_text(all_rows[0])),
        "parties": parties,
        "spots": spots,
        "dates": dates,
        "names": names,
        "red_flags": flags,
        "notes": notes,
        "signatures": state["signatures"],
        "lock_level": state["lock_level"],
        "mode": state["mode"],
        "scanned_pages": scanned,
        "seconds": round(time.time() - started, 2),
    }


# =============================================================================
# Sign: where exactly the stamps go
# =============================================================================

def placement_for_spot(spot, date_id, page, analysis, aspect, date_text):
    """The stamp (and date) for one chosen spot on a freshly read page. Raises ValueError when the
    signature can't go there. A date that doesn't fit is left off: 'date_left_off' then says why."""
    signature = place_signature(page, spot, aspect)
    if not signature["fits"]:
        raise ValueError(f"Spot {spot['id']}: {signature['reason']}")
    empty, why = spot_is_empty(page, spot, signature["rect"])
    if not empty:
        raise ValueError(f"Spot {spot['id']}: {why}")
    placement = {"page": spot["page"], "signature": signature["rect"], "spot_id": spot["id"], "date": None,
                 "date_id": None, "date_left_off": None, "manual": False}
    if date_id:
        date = find_by_id(analysis["dates"], date_id)
        if date is None:
            raise ValueError(f"Date spot {date_id} doesn't exist")
        if date["page"] != spot["page"]:
            raise ValueError(f"Date spot {date['id']} isn't on the same page as spot {spot['id']}")
        placed = place_date(page, date, date_text)
        if placed["fits"]:
            placement["date"] = placed
            placement["date_id"] = date["id"]
        else:
            placement["date_left_off"] = placed["reason"]
    return placement


def placements_for_spots(analysis, choices, pdf_path, aspect, date_text):
    """Exact stamp and date positions for the chosen spots, recomputed from a fresh read of the PDF.

    `choices` is a list of {"spot": id, "date": id or None}. Raises ValueError with a reason a
    person can act on when a spot can't take a stamp.
    """
    placements = []
    pages = {}          # page number -> that page, read once
    with pymupdf.open(pdf_path) as doc:
        for choice in choices:
            spot = find_by_id(analysis["spots"], choice["spot"])
            if spot is None or spot["kind"] != "signature":
                raise ValueError(f"Spot {choice['spot']} is not a signature spot")
            if spot["page"] not in pages:
                pages[spot["page"]] = read_page(doc, spot["page"] - 1)
            page = pages[spot["page"]]
            placements.append(placement_for_spot(spot, choice.get("date"), page, analysis, aspect, date_text))
    return placements


def manual_placement(pdf_path, page_number, x, y, date_point, aspect):
    """A place clicked by hand: the stamp centred where the person clicked, the date where they clicked next."""
    with pymupdf.open(pdf_path) as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"Page {page_number} doesn't exist")
        page = doc[page_number - 1]
        if page.rotation:
            raise ValueError("Placing by hand isn't supported on rotated pages")
        page_width = page.rect.width
        page_height = page.rect.height
    height = SIGNATURE_HEIGHTS[0]
    width = height * aspect
    x0 = min(max(x - width / 2, 2), page_width - width - 2)          # kept 2 pt inside the page
    y0 = min(max(y - height / 2, 2), page_height - height - 2)
    placement = {"page": page_number, "signature": rounded([x0, y0, x0 + width, y0 + height]), "spot_id": None,
                 "date": None, "date_left_off": None, "manual": True}
    if date_point:
        date_x, date_y = date_point
        placement["date"] = {"fits": True, "kind": "text", "point": [round(date_x, 1), round(date_y, 1)],
                             "rect": rounded([date_x, date_y - 8, date_x + 130, date_y + 2]),
                             "size": DATE_FONT_SIZES[0]}
    return placement
