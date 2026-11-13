"""
The AutoSign workflow, on top of the pipeline:

    detect()           find where the client signs and where the company signs (rules + AI)
    check_places()     check the places after the company changed them on the agreement page
    presign()          stamp the company's signature once; every client signs that pre-signed copy
    sign()             stamp the client's signature on the pre-signed copy
    add_certificate()  append the page that records how the agreement was signed

The pipeline judges every block from the side of one party, "ours". Here that party is the
client, so the pipeline's "ours" means "the client's", and the company sending the agreement is
passed in as the other party.

Who signs where is kept as places: {"choices": [{"spot": id, "date": id or None}, ...],
"manual": [{"page": n, "x": x, "y": y, "date": [x, y] or None}, ...]}. The choices are spots
AutoSign found; the manual places were clicked by hand on a page. Placements are the exact
positions of the stamps and dates for some places.
"""

import io
import os
import time
from pathlib import Path

import pymupdf
from PIL import Image

from autosign import ai, pipeline
from autosign.policy import CLIENT_ROLES, DATE_FORMAT, KNOWN_ROLES, SIGNATURE_ASPECT

ONLY_THE_CLIENT_SIGNS = ("No place is set for your company's signature, so only the client will sign. "
                         "To sign for your company too, choose its place below.")


class SigningError(Exception):
    """Signing failed in a way the person signing should be told about."""


def client_settings(firm_name):
    """Pipeline settings for finding the client's blocks in an agreement sent by `firm_name`."""
    return {"company_name": "", "company_aliases": [], "signer_name": "", "usual_roles": CLIENT_ROLES,
            "other_party_names": [firm_name], "date_format": DATE_FORMAT}


def date_today(tz_offset_minutes=0):
    """Today's date where the signer is (the browser sends its UTC offset; clamped to real time zones)."""
    offset = int(tz_offset_minutes or 0)
    offset = max(-14 * 60, min(14 * 60, offset))
    return time.strftime(DATE_FORMAT, time.gmtime(time.time() + offset * 60))


def spots_by_id(analysis):
    return {spot["id"]: spot for spot in analysis["spots"]}


def describe(spot):
    """'page 4: CLIENT - Signature'."""
    label = spot["label"] or "signature"
    header = ""
    if spot.get("header"):
        header = f"{spot['header']} - "
    return f"page {spot['page']}: {header}{label}"


def ids_text(spot_ids):
    if not spot_ids:
        return "none"
    return ", ".join(str(spot_id) for spot_id in spot_ids)


# =============================================================================
# Places
# =============================================================================

def no_places():
    return {"choices": [], "manual": []}


def places_at(spot_ids, spots):
    """Places for some spots, each with the date of its block."""
    choices = []
    for spot_id in spot_ids:
        choices.append({"spot": spot_id, "date": spots[spot_id]["date_id"]})
    return {"choices": choices, "manual": []}


def has_places(places):
    return bool(places.get("choices") or places.get("manual"))


def chosen_spot_ids(places):
    return [choice["spot"] for choice in places.get("choices") or []]


def describe_places(analysis, places):
    """'page 4: CLIENT - By:; page 2 (placed by hand)', or '' when there are none."""
    spots = spots_by_id(analysis)
    parts = []
    for spot_id in chosen_spot_ids(places):
        parts.append(describe(spots[spot_id]))
    for place in places.get("manual") or []:
        parts.append(f"page {place['page']} (placed by hand)")
    return "; ".join(parts)


def summarize(analysis, client, company):
    """One line for the agreement page: where the client signs, and where the company signs."""
    client_text = describe_places(analysis, client)
    if client_text:
        summary = f"The client signs on {client_text}."
    else:
        summary = "No place is set for the client's signature yet."
    company_text = describe_places(analysis, company)
    if company_text:
        summary += f" Your company signs on {company_text}."
    return summary


def check_overlaps(placements):
    """Raises ValueError when two stamps would cover each other."""
    for index, first in enumerate(placements):
        for second in placements[index + 1:]:
            if first["page"] != second["page"]:
                continue
            if pipeline.intersects(first["signature"], second["signature"], pad=0):
                raise ValueError(f"two signatures would overlap on page {first['page']}")


def placements_for(analysis, places, pdf_path, date_text=None):
    """Exact stamp and date positions for some places: the chosen spots, then the places added by hand.

    Raises ValueError when a stamp doesn't fit, or two of them would overlap.
    """
    if date_text is None:
        date_text = date_today()
    choices = places.get("choices") or []
    placements = pipeline.placements_for_spots(analysis, choices, pdf_path, SIGNATURE_ASPECT, date_text)
    for place in places.get("manual") or []:
        placement = pipeline.manual_placement(pdf_path, place["page"], place["x"], place["y"], place["date"],
                                              SIGNATURE_ASPECT)
        placements.append(placement)
    check_overlaps(placements)
    return placements


# =============================================================================
# Finding where the client and the company sign
# =============================================================================

def firm_identity(analysis, firm_name):
    """The sending company's names and its role, as the agreement's opening defines them."""
    opening = analysis["parties"]["opening_text"]
    known = set()
    for role in KNOWN_ROLES + CLIENT_ROLES:
        known.add(pipeline.norm(role))
    firm = pipeline.find_party(opening, opening, [firm_name], known)
    return [firm_name] + firm["aliases"], firm["role"]


def is_firm_block(spot, names, role):
    """A block is the sending company's when it shows the company's name, or the role the agreement gives it."""
    text = " ".join([spot["block_text"] or "", spot["label"] or "", spot.get("header") or ""])
    if pipeline.mentions_any(text, names):
        return True
    if role and spot.get("role"):
        return pipeline.norm(spot["role"]) == pipeline.norm(role)
    return False


def signable_spots(analysis):
    """Signature spots that someone in the agreement could sign (never a witness or notary block)."""
    return [spot for spot in analysis["spots"] if spot["kind"] == "signature" and spot["owner"] != "never"]


def find_firm_spots(analysis, firm_name):
    """The spots the rules say are the company's."""
    names, role = firm_identity(analysis, firm_name)
    firm_ids = []
    for spot in signable_spots(analysis):
        if spot["owner"] != "ours" and is_firm_block(spot, names, role):
            firm_ids.append(spot["id"])
    return firm_ids


def find_client_spots_by_rules(analysis):
    """The pipeline's 'ours' is the client here (see client_settings)."""
    return [spot["id"] for spot in signable_spots(analysis) if spot["owner"] == "ours"]


def spots_picked_by_ai(opinion, key, spots, excluded):
    """The AI's picks ('picks' for the client, 'company_picks' for the company), without the spots they
    can't be: witness and notary spots, and the other side's spots (`excluded`)."""
    if not opinion.get("ok"):
        return []
    picked = []
    for pick in opinion["answer"].get(key) or []:
        spot = spots.get(pick["signature_spot"])
        if spot is None or spot["kind"] != "signature" or spot["owner"] == "never":
            continue
        if spot["id"] in excluded:
            continue
        picked.append(spot["id"])
    return picked


def choose_client_spots(analysis, rules_ids, ai_ids, firm_ids):
    """Where the client signs: the rules' pick, else the blocks that aren't the company's, else the AI's.

    Returns (spot ids, True when only the AI found them).
    """
    if rules_ids:
        return rules_ids, False
    other_side = []
    for spot in signable_spots(analysis):
        if spot["id"] not in firm_ids:
            other_side.append(spot["id"])
    if firm_ids and other_side:
        return other_side, False
    return ai_ids, bool(ai_ids)


def choose_company_spots(rules_ids, ai_ids):
    """Where the company signs: the rules' pick, else the AI's. Returns (spot ids, True when only the AI found them)."""
    if rules_ids:
        return rules_ids, False
    return ai_ids, bool(ai_ids)


def compare_with_ai(chosen, ai_ids, only_the_ai, whose):
    """The reason to check one side's places, if any: the AI picked other places, or only the AI found them."""
    if sorted(chosen) != sorted(ai_ids):
        return [f"The rules and the AI picked different places for {whose} "
                f"(rules: {ids_text(chosen)}; AI: {ids_text(ai_ids)})."]
    if only_the_ai:
        return [f"Only the AI found the place for {whose}; the rules found none. Check it before sending."]
    return []


def document_reasons(analysis, opinion):
    """Reasons a person must look at the document itself."""
    reasons = []
    for flag in analysis["red_flags"]:
        reasons.append(f"Check the document: {flag}")
    if not opinion.get("ok"):
        reasons.append(f"The AI check didn't run: {opinion.get('error')}")
    return reasons


def page_break_reasons(analysis, spot_ids):
    """A block named only at the bottom of the page before it may be a notice address: a person checks it."""
    spots = spots_by_id(analysis)
    reasons = []
    for spot_id in spot_ids:
        if spots[spot_id]["evidence"] == "page_break":
            reasons.append(f"Spot {spot_id} is only named at the bottom of the page before it: check whose it is.")
    return reasons


def detect(pdf_path, firm_name):
    """Rules and AI each decide where the client signs and where the company signs.

    Returns the analysis, the AI's opinion, the client's and the company's places with their
    placements, a one-line summary, and 'reasons': what a person must check before any signing
    link is created. No reasons means the rules and the AI agree, every stamp fits, and nothing
    in the document needs a person's eye. Raises ValueError when the file can't be used at all.
    """
    analysis = pipeline.analyze(pdf_path, client_settings(firm_name), SIGNATURE_ASPECT)
    opinion = ai.ask_ai(pdf_path, analysis, firm_name)
    spots = spots_by_id(analysis)

    firm_ids = find_firm_spots(analysis, firm_name)
    client_by_rules = find_client_spots_by_rules(analysis)
    client_by_ai = spots_picked_by_ai(opinion, "picks", spots, firm_ids)
    client_ids, client_only_ai = choose_client_spots(analysis, client_by_rules, client_by_ai, firm_ids)

    company_by_rules = [spot_id for spot_id in firm_ids if spot_id not in client_ids]
    company_by_ai = spots_picked_by_ai(opinion, "company_picks", spots, client_ids)
    company_ids, company_only_ai = choose_company_spots(company_by_rules, company_by_ai)

    reasons = document_reasons(analysis, opinion)
    if not client_ids:
        reasons.append("Couldn't tell where the client signs. Choose the client's place below.")
    elif opinion.get("ok"):
        reasons += compare_with_ai(client_ids, client_by_ai, client_only_ai, "the client's signature")
    if company_ids and opinion.get("ok"):
        reasons += compare_with_ai(company_ids, company_by_ai, company_only_ai, "your company's signature")
    reasons += page_break_reasons(analysis, client_ids)

    client = places_at(client_ids, spots)
    try:
        client_placements = placements_for(analysis, client, pdf_path)
    except ValueError as error:
        reasons.append(f"The client's signature doesn't fit at its place ({error}). Choose another place below.")
        client = no_places()
        client_placements = []

    company = places_at(company_ids, spots)
    try:
        company_placements = placements_for(analysis, company, pdf_path)
        check_overlaps(client_placements + company_placements)
    except ValueError as error:
        reasons.append(f"Your company's signature doesn't fit at its place ({error}). Choose another place below.")
        company = no_places()
        company_placements = []

    company["placements"] = company_placements
    company["note"] = None
    if not has_places(company):
        company["note"] = ONLY_THE_CLIENT_SIGNS

    return {
        "analysis": analysis,
        "ai": opinion,
        "client": client,
        "client_placements": client_placements,
        "company": company,
        "reasons": reasons,
        "summary": summarize(analysis, client, company),
    }


# =============================================================================
# Checking the places a person chose
# =============================================================================

def clean_manual_place(place, page_count):
    page = int(place["page"])
    if not 1 <= page <= page_count:
        raise ValueError(f"page {page} doesn't exist")
    date = None
    if place.get("date"):
        date = [float(place["date"][0]), float(place["date"][1])]
    return {"page": page, "x": float(place["x"]), "y": float(place["y"]), "date": date}


def clean_places(analysis, raw):
    """Places as the agreement page sent them, checked against the analysis (ValueError when wrong)."""
    raw = raw or {}
    spots = spots_by_id(analysis)
    choices = []
    for choice in raw.get("choices") or []:
        spot = spots.get(int(choice["spot"]))
        if spot is None or spot["kind"] != "signature":
            raise ValueError(f"spot {choice['spot']} is not a signature place")
        choices.append({"spot": spot["id"], "date": spot["date_id"]})
    manual = []
    for place in raw.get("manual") or []:
        manual.append(clean_manual_place(place, analysis["page_count"]))
    return {"choices": choices, "manual": manual}


def check_each_spot_once(client, company):
    """A spot is signed by the client or by the company, and only once. Raises ValueError otherwise."""
    seen = set()
    for spot_id in chosen_spot_ids(client) + chosen_spot_ids(company):
        if spot_id in seen:
            raise ValueError(f"spot {spot_id} is chosen twice; each place is signed by one side only")
        seen.add(spot_id)


def check_places(analysis, edited, pdf_path):
    """Check the places a person chose on the agreement page.

    `edited` is {"client": places, "company": places}. The client needs at least one place; the
    company may have none (then only the client signs). Returns (client, client_placements,
    company, company_placements). Raises ValueError with a reason the person can act on.
    """
    client = clean_places(analysis, edited.get("client"))
    company = clean_places(analysis, edited.get("company"))
    check_each_spot_once(client, company)
    if not has_places(client):
        raise ValueError("choose at least one place where the client signs")
    client_placements = placements_for(analysis, client, pdf_path)
    company_placements = placements_for(analysis, company, pdf_path)
    check_overlaps(client_placements + company_placements)
    return client, client_placements, company, company_placements


# =============================================================================
# Signing: the company once, then each client
# =============================================================================

def opacity_for(gray):
    """How opaque a pixel of this grey level (0 black, 255 white) becomes: near-white is see-through."""
    if gray > 225:
        return 0
    return min(255, (225 - gray) * 3)


def clear_background(image):
    """An image with no transparency gets it, so the stamp never hides the line it sits on."""
    if image.getchannel("A").getextrema()[0] < 255:
        return image
    image.putalpha(image.convert("L").point(opacity_for))
    return image


def pad_to_aspect(image):
    """The image on a transparent canvas shaped SIGNATURE_ASPECT: a wide signature gets space above and
    below, any other gets space on the right (it stays left-aligned)."""
    width, height = image.size
    if width / height > SIGNATURE_ASPECT:
        canvas_size = (width, round(width / SIGNATURE_ASPECT))
        position = (0, (canvas_size[1] - height) // 2)
    else:
        canvas_size = (round(height * SIGNATURE_ASPECT), height)
        position = (0, 0)
    canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    canvas.paste(image, position, image)
    return canvas


def signature_png(data):
    """A signature image ready to stamp, as PNG bytes: background cleared, cropped to the ink, padded.

    Every stamp place was sized for SIGNATURE_ASPECT, so a padded signature always fits its place
    exactly: it is never stretched, and never too wide for its line. The image file itself is
    never changed.
    """
    try:
        image = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as error:
        raise SigningError(f"The signature image can't be read ({error})")
    image = clear_background(image)
    ink = image.getchannel("A").getbbox()
    if ink is None:
        raise SigningError("The signature image is empty once its white background is removed")
    image = image.crop(ink)
    if image.width > 1200:
        image = image.resize((1200, max(1, round(image.height * 1200 / image.width))))
    if image.width < 40 or image.height < 8:
        raise SigningError("The signature is too small. Add your full signature.")
    out = io.BytesIO()
    pad_to_aspect(image).save(out, "PNG")
    return out.getvalue()


def failed_checks(result):
    titles = []
    for check in result["checks"]:
        if not check["passed"]:
            titles.append(check["title"])
    return ", ".join(titles)


def presign(original, analysis, company, client_placements, signature_image, signer_name, date_text, out_path):
    """Stamp the company's signature (and the date) at its places, on the copy every client will sign.

    Returns the placements. Raises SigningError when the company's signature can't go on; the
    original agreement is never changed either way.
    """
    try:
        png = signature_png(signature_image)
    except SigningError as error:
        raise SigningError(f"Your company's signature image can't be used: {error}")
    try:
        placements = placements_for(analysis, company, str(original), date_text)
        check_overlaps(placements + client_placements)
    except ValueError as error:
        raise SigningError(f"Your company's signature doesn't fit at its place ({error})")
    result = pipeline.sign_pdf(str(original), str(out_path), placements, png, date_text, analysis["mode"],
                               signer_name)
    if not result["ok"]:
        raise SigningError(f"The copy with your company's signature failed its checks ({failed_checks(result)})")
    return placements


def sign(pdf_path, analysis, places, image, signer_name, date_text, out_path):
    """Stamp the client's signature (and the date) at every place. Returns (placements, the stamped PNG)."""
    png = signature_png(image)
    try:
        placements = placements_for(analysis, places, pdf_path, date_text)
    except ValueError as error:
        raise SigningError(f"The signature places no longer fit this document ({error})")
    result = pipeline.sign_pdf(pdf_path, out_path, placements, png, date_text, analysis["mode"], signer_name)
    if not result["ok"]:
        raise SigningError(f"The signed copy failed its checks ({failed_checks(result)}). Nothing was signed.")
    return placements, png


# =============================================================================
# The signing certificate: a last page that records how the agreement was signed
# =============================================================================

CONSENT_TEXT = ("I agree to sign this agreement electronically, and that my electronic signature has the same "
                "effect as my handwritten signature.")
CERTIFICATE_GREY = (0.35, 0.38, 0.45)
CERTIFICATE_INK = (0.08, 0.1, 0.15)


def write_certificate_row(page, left, right, y, label, value, monospace):
    """One label and its value. Returns the height where the next row starts."""
    page.insert_text((left, y + 8), label, fontsize=8.5, fontname="helv", color=CERTIFICATE_GREY)
    if monospace:
        font = "cour"
        size = 8
    else:
        font = "helv"
        size = 10
    box = pymupdf.Rect(left + 150, y, right, y + 60)
    page.insert_textbox(box, value, fontsize=size, fontname=font, color=CERTIFICATE_INK)
    width = pymupdf.get_text_length(value, fontname=font, fontsize=size)
    lines = int(width // box.width) + 1
    return y + max(18, lines * (size + 3) + 6)


def write_certificate(page, rows, signatures):
    left = 54
    right = page.rect.width - 54
    y = 64
    page.insert_text((left, y), "Signing certificate", fontsize=18, fontname="hebo", color=(0.1, 0.15, 0.3))
    y += 18
    intro = "Added by AutoSign when this agreement was signed. It records who signed, when, and how."
    page.insert_textbox(pymupdf.Rect(left, y, right, y + 30), intro, fontsize=9, fontname="helv",
                        color=CERTIFICATE_GREY)
    y += 34
    for label, value, monospace in rows:
        y = write_certificate_row(page, left, right, y, label, str(value), monospace)
    for label, png in signatures:
        y += 6
        page.insert_text((left, y + 8), label, fontsize=8.5, fontname="helv", color=CERTIFICATE_GREY)
        box = pymupdf.Rect(left + 150, y - 4, left + 150 + 44 * SIGNATURE_ASPECT, y + 40)
        page.insert_image(box, stream=png, keep_proportion=True)
        y += 50


def add_certificate(signed_path, rows, signatures, mode):
    """Append the certificate page to the signed PDF. Returns False when the PDF must keep its bytes.

    `rows` are (label, value, monospace) lines; `signatures` are (label, PNG) pairs shown below them.
    A PDF that already carries digital signatures is only ever changed append-only, so no page is
    added there; the same details are in the audit log either way.
    """
    if mode != "content":
        return False
    signed_path = Path(signed_path)
    work = signed_path.with_name(signed_path.stem + ".work.certificate.pdf")
    with pymupdf.open(signed_path) as doc:
        size = doc[0].rect
        page = doc.new_page(width=size.width, height=size.height)
        write_certificate(page, rows, signatures)
        doc.save(work, garbage=3, deflate=True)
    os.replace(work, signed_path)
    return True


# =============================================================================
# Page pictures
# =============================================================================

PICTURE_DPI = 110
CLIENT_GREEN = (0, 0.55, 0.2)
COMPANY_BLUE = (0.1, 0.35, 0.85)
OTHER_GREY = (0.45, 0.45, 0.45)
SIGN_HERE_ORANGE = (0.85, 0.5, 0)


def page_of(doc, page_number):
    if not 1 <= page_number <= doc.page_count:
        raise ValueError(f"Page {page_number} doesn't exist")
    return doc[page_number - 1]


def outline(page, area, number, color):
    """A dashed box with its number in the top-right corner."""
    page.draw_rect(pymupdf.Rect(area), color=color, width=0.9, dashes="[2 2] 0")
    page.insert_text((area[2] - 12, area[1] + 8), str(number), fontsize=8, color=color)


def draw_place(page, placement, color, label):
    """A tinted box where a signature will go, with a label, and a lighter one where its date goes."""
    rect = pymupdf.Rect(placement["signature"])
    page.draw_rect(rect, color=color, fill=color, fill_opacity=0.2, width=1.2)
    page.insert_text((rect.x0 + 3, rect.y0 + 10), label, fontsize=8, color=color)
    date = placement.get("date")
    if date and date.get("rect"):
        page.draw_rect(pymupdf.Rect(date["rect"]), color=color, fill=color, fill_opacity=0.1, width=0.8)


def spot_color(spot_id, client_ids, company_ids):
    if spot_id in client_ids:
        return CLIENT_GREEN
    if spot_id in company_ids:
        return COMPANY_BLUE
    return OTHER_GREY


def render_for_company(pdf_path, page_number, analysis, client, client_placements, company):
    """A page as the company checks it. Every signature spot is outlined and numbered (green: the
    client's, blue: the company's, grey: left empty), and each place is drawn in. The company's
    places are drawn until its signature is on the page."""
    client_ids = chosen_spot_ids(client)
    company_ids = chosen_spot_ids(company)
    with pymupdf.open(pdf_path) as doc:
        page = page_of(doc, page_number)
        for spot in analysis["spots"]:
            if spot["page"] == page_number and spot["kind"] == "signature":
                outline(page, spot["area"], spot["id"], spot_color(spot["id"], client_ids, company_ids))
        for placement in client_placements:
            if placement["page"] == page_number:
                draw_place(page, placement, CLIENT_GREEN, "the client")
        if not company.get("signed_at"):
            for placement in company.get("placements") or []:
                if placement["page"] == page_number:
                    draw_place(page, placement, COMPANY_BLUE, "your company")
        return page.get_pixmap(dpi=PICTURE_DPI).tobytes("png")


def render_for_signer(pdf_path, page_number, placements):
    """A page as the client reads it, with each place to sign highlighted."""
    with pymupdf.open(pdf_path) as doc:
        page = page_of(doc, page_number)
        for placement in placements:
            if placement["page"] == page_number:
                draw_place(page, placement, SIGN_HERE_ORANGE, "Sign here")
        return page.get_pixmap(dpi=PICTURE_DPI).tobytes("png")


def render_plain(pdf_path, page_number):
    with pymupdf.open(pdf_path) as doc:
        return page_of(doc, page_number).get_pixmap(dpi=PICTURE_DPI).tobytes("png")


def page_count(pdf_path):
    with pymupdf.open(pdf_path) as doc:
        return doc.page_count
