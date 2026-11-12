"""
Test support: the test documents (each with a known right answer) and the helpers the tests share.
"""

import datetime
import time
from pathlib import Path

import pymupdf

from autosign import pipeline, storage

# =============================================================================
# Test documents: each function writes one PDF with a known right answer
# =============================================================================

DATA = Path(__file__).resolve().parents[1] / "data"     # your sample contracts (the project's data/ folder)


def make_test_signature(path):
    """A synthetic squiggle on a transparent background (the tests never use your real signature)."""
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (600, 180), (0, 0, 0, 0))
    points = [(20, 120), (90, 40), (150, 140), (230, 50), (300, 130), (380, 60), (460, 125), (580, 70)]
    ImageDraw.Draw(image).line(points, fill=(20, 40, 120, 255), width=9, joint="curve")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")


def paragraph(page, y, text, size=11):
    """Write a wrapped paragraph from y; return the y below it.

    PyMuPDF's built-in Helvetica has no curly quotes (they come out as '?'), so the generated
    documents use straight quotes. Curly quotes are covered by the Service Contract sample.
    """
    text = text.replace("“", '"').replace("”", '"')
    rect = pymupdf.Rect(72, y, 540, y + 200)
    spare = page.insert_textbox(rect, text, fontsize=size, fontname="helv")
    return y + 200 - spare + 10


def signature_rows(page, x, y, header, labels=("By:", "Name:", "Title:", "Date:"), width=170):
    """A signature block: a bold header, then label + line rows."""
    page.insert_text((x, y), header, fontsize=11, fontname="hebo")
    for i, label in enumerate(labels):
        row_y = y + 30 + i * 28
        page.insert_text((x, row_y), label, fontsize=11, fontname="helv")
        page.draw_line((x + 40, row_y + 1), (x + 40 + width, row_y + 1), color=(0, 0, 0), width=0.8)
    return y + 30 + len(labels) * 28


def side_by_side(folder, name="side_by_side.pdf", tag="", extra=None, rotate_signature_page=False):
    """Service agreement, two pages. Page 2: CLIENT and SERVICE PROVIDER blocks side by side."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "SERVICE AGREEMENT", fontsize=16, fontname="hebo")
    y = paragraph(page, 100, "This Service Agreement (the “Agreement”) is made on November 1, 2026 between "
                             "Foo Holdings LLC, a Texas limited liability company (the “Client”), and Acme "
                             "Analytics Inc., a Delaware corporation (the “Service Provider”).")
    y = paragraph(page, y, "1. Services. The Service Provider will provide data analysis services to the Client "
                           "as described in each statement of work. The Client will pay the fees within 30 days.")
    if tag:
        page.insert_text((72, 700), f"Reference: {tag}", fontsize=9)
    if extra:
        extra(page)
    page2 = doc.new_page(width=612, height=792)
    paragraph(page2, 80, "IN WITNESS WHEREOF, the parties have signed this Agreement on the dates below.")
    signature_rows(page2, 72, 160, "CLIENT")
    signature_rows(page2, 330, 160, "SERVICE PROVIDER")
    if rotate_signature_page:
        page2.set_rotation(90)
    path = Path(folder) / name
    doc.save(path)
    return str(path)


def company_trap(folder):
    """'Company' is the client here. Your company is the Consultant. Blocks are stacked."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "CONSULTING AGREEMENT", fontsize=16, fontname="hebo")
    paragraph(page, 100, "This Consulting Agreement is made between XYZ Corp., a Delaware corporation "
                         "(the “Company”), and Acme Analytics Inc. (the “Consultant”).")
    page2 = doc.new_page(width=612, height=792)
    paragraph(page2, 80, "IN WITNESS WHEREOF, the parties have executed this Agreement.")
    y = signature_rows(page2, 72, 150, "COMPANY", labels=("By:", "Name:", "Title:"))
    signature_rows(page2, 72, y + 44, "CONSULTANT")
    path = Path(folder) / "company_trap.pdf"
    doc.save(path)
    return str(path)


def witness_doc(folder):
    """Your block is followed by a witness line that must never be signed."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "MUTUAL NON-DISCLOSURE AGREEMENT", fontsize=16, fontname="hebo")
    paragraph(page, 100, "This Mutual Non-Disclosure Agreement is made between Foo Holdings LLC "
                         "(“Party A”) and Acme Analytics Inc. (“Party B”).")
    y = signature_rows(page, 72, 300, "PARTY A: FOO HOLDINGS LLC", labels=("Signature:", "Name:", "Date:"))
    y = signature_rows(page, 72, y + 40, "PARTY B: ACME ANALYTICS INC.", labels=("Signature:", "Name:", "Date:"))
    page.insert_text((72, y + 40), "Witness:", fontsize=11)
    page.draw_line((112, y + 41), (282, y + 41), color=(0, 0, 0), width=0.8)
    path = Path(folder) / "witness.pdf"
    doc.save(path)
    return str(path)


def signature_field_doc(folder):
    """A PDF with an empty signature form field for your company (added with pyHanko)."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "STATEMENT OF WORK", fontsize=16, fontname="hebo")
    paragraph(page, 100, "This Statement of Work is entered into by Foo Holdings LLC (the “Client”) and "
                         "Acme Analytics Inc. (the “Provider”).")
    page.insert_text((72, 330), "PROVIDER", fontsize=11, fontname="hebo")
    page.insert_text((72, 372), "Signature:", fontsize=11)
    base = Path(folder) / "sigfield_base.pdf"
    doc.save(base)
    out = Path(folder) / "signature_field.pdf"
    with open(base, "rb") as inf:
        writer = IncrementalPdfFileWriter(inf)
        # pyHanko boxes use PDF coordinates (origin bottom-left): y 792-375=417 to 792-340=452.
        fields.append_signature_field(writer, fields.SigFieldSpec("ProviderSignature", box=(130, 417, 330, 452), on_page=0))
        with open(out, "wb") as outf:
            writer.write(outf)
    return str(out)


def box_doc(folder):
    """A signature box: an outlined rectangle with 'Signature' printed inside."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "NON-DISCLOSURE AGREEMENT", fontsize=16, fontname="hebo")
    paragraph(page, 100, "This Agreement is made between Foo Holdings LLC (the “Disclosing Party”) and "
                         "Acme Analytics Inc. (the “Recipient”).")
    page.insert_text((72, 330), "RECIPIENT: ACME ANALYTICS INC.", fontsize=11, fontname="hebo")
    page.draw_rect(pymupdf.Rect(72, 340, 300, 400), color=(0, 0, 0), width=0.8)
    page.insert_text((76, 352), "Signature", fontsize=8)
    path = Path(folder) / "box.pdf"
    doc.save(path)
    return str(path)


def page_break_doc(folder):
    """Your block's header is at the bottom of page 2; its 'By:' line is at the top of page 3."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "MASTER SERVICES AGREEMENT", fontsize=16, fontname="hebo")
    paragraph(page, 100, "This Master Services Agreement is made between Foo Holdings LLC (the “Customer”) "
                         "and Acme Analytics Inc. (the “Provider”).")
    page2 = doc.new_page(width=612, height=792)
    paragraph(page2, 80, "Terms continue here. The parties agree to the terms above.")
    page2.insert_text((72, 740), "ACME ANALYTICS INC.", fontsize=11, fontname="hebo")
    page3 = doc.new_page(width=612, height=792)
    for i, label in enumerate(("By:", "Name:", "Title:")):
        page3.insert_text((72, 80 + i * 28), label, fontsize=11)
        page3.draw_line((112, 81 + i * 28), (282, 81 + i * 28), color=(0, 0, 0), width=0.8)
    signature_rows(page3, 72, 220, "FOO HOLDINGS LLC", labels=("By:", "Name:", "Title:"))
    path = Path(folder) / "page_break.pdf"
    doc.save(path)
    return str(path)


def scanned_doc(folder):
    """An image-only PDF, like a scan: no text layer at all."""
    source = pymupdf.open(side_by_side(folder, "scan_source.pdf"))
    doc = pymupdf.open()
    for page in source:
        png = page.get_pixmap(dpi=100).tobytes("png")
        new = doc.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=png)
    path = Path(folder) / "scanned.pdf"
    doc.save(path)
    return str(path)


def encrypted_doc(folder):
    doc = pymupdf.open(side_by_side(folder, "plain.pdf"))
    path = Path(folder) / "encrypted.pdf"
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner-secret")
    return str(path)


def filled_service_contract(folder, tag=""):
    """Your Service-Contract-Template.pdf with the party names typed into its form fields."""
    doc = pymupdf.open(DATA / "Service-Contract-Template.pdf")
    values = {"with a mailing address of": "Acme Analytics Inc.", "with a mailing address of_2": "Foo Holdings LLC",
              "Service Provider and": "12 Main St, Boston MA", "Client": "99 Elm Rd, Austin TX " + tag}
    for page in doc:
        for widget in page.widgets():
            if widget.field_name in values:
                widget.field_value = values[widget.field_name]
                widget.update()
    path = Path(folder) / f"service_filled{tag}.pdf"
    doc.save(path)
    return str(path)


def make_test_certificate(folder):
    """A self-signed certificate for 'Test Client', like the one a client's e-sign tool would use."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Client")])
    now = datetime.datetime.now(datetime.timezone.utc)
    usage = x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                          encipher_only=False, decipher_only=False)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365)).add_extension(usage, critical=True)
            .sign(key, hashes.SHA256()))
    key_file, cert_file = Path(folder) / "client-key.pem", Path(folder) / "client-cert.pem"
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(key_file), str(cert_file)


def client_signed(folder, level):
    """The side-by-side agreement, digitally signed by the client first.

    level: "approval" (an ordinary signature), or 1, 2, 3 (a certification with that lock level).
    """
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers
    from pyhanko.sign.fields import MDPPerm
    source = side_by_side(folder, f"to_sign_{level}.pdf", tag=f"client-signed-{level}")
    key_file, cert_file = make_test_certificate(folder)
    signer = signers.SimpleSigner.load(key_file, cert_file)
    permissions = {1: MDPPerm.NO_CHANGES, 2: MDPPerm.FILL_FORMS, 3: MDPPerm.ANNOTATE}
    meta = signers.PdfSignatureMetadata(field_name="ClientSignature", certify=level != "approval",
                                        docmdp_permissions=permissions.get(level, MDPPerm.FILL_FORMS))
    out = Path(folder) / f"client_signed_{level}.pdf"
    with open(source, "rb") as inf:
        writer = IncrementalPdfFileWriter(inf)
        # Visible signature over the client's "By:" line (page 2, x 112-282, y 190): PDF y = 792 - y.
        fields.append_signature_field(writer, fields.SigFieldSpec("ClientSignature", box=(112, 602, 282, 628), on_page=1))
        with open(out, "wb") as outf:
            signers.sign_pdf(writer, meta, signer=signer, output=outf)
    return str(out)


def multi_block_doc(folder, tag=""):
    """A master services agreement signed twice by each party: the main agreement and Exhibit A (an SOW)."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "MASTER SERVICES AGREEMENT", fontsize=16, fontname="hebo")
    paragraph(page, 100, "This Master Services Agreement is made between Foo Holdings LLC (the “Client”) and "
                         "Acme Analytics Inc. (the “Service Provider”). Exhibit A is the first statement of work.")
    if tag:
        page.insert_text((72, 700), f"Reference: {tag}", fontsize=9)
    for title in ("SIGNATURES", "EXHIBIT A: STATEMENT OF WORK No. 1"):
        sign_page = doc.new_page(width=612, height=792)
        sign_page.insert_text((72, 80), title, fontsize=12, fontname="hebo")
        paragraph(sign_page, 100, "The parties agree to the terms above.")
        signature_rows(sign_page, 72, 180, "CLIENT")
        signature_rows(sign_page, 330, 180, "SERVICE PROVIDER")
    path = Path(folder) / f"multi_block{tag}.pdf"
    doc.save(path)
    return str(path)


def unnamed_party_doc(folder):
    """The client is named; the Service Provider's name is a blank line. The blank party is you."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "SERVICE AGREEMENT", fontsize=16, fontname="hebo")
    paragraph(page, 100, "This Service Agreement is made between Foo Holdings LLC (the “Client”) and")
    page.insert_text((72, 140), "Service Provider:", fontsize=11)
    page.draw_line((170, 141), (400, 141), color=(0, 0, 0), width=0.8)
    paragraph(page, 160, "(the “Service Provider”). The Service Provider will provide the services.")
    page2 = doc.new_page(width=612, height=792)
    signature_rows(page2, 72, 160, "CLIENT")
    signature_rows(page2, 330, 160, "SERVICE PROVIDER")
    path = Path(folder) / "unnamed_party.pdf"
    doc.save(path)
    return str(path)


def two_usual_roles_doc(folder):
    """A one-way NDA with both names blank: Disclosing Party and Receiving Party are both roles you usually have."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "NON-DISCLOSURE AGREEMENT", fontsize=16, fontname="hebo")
    paragraph(page, 100, "This Agreement is made between the party named below as the Disclosing Party "
                         "(the “Disclosing Party”) and the party named below as the Receiving Party (the “Receiving Party”).")
    signature_rows(page, 72, 300, "DISCLOSING PARTY")
    signature_rows(page, 330, 300, "RECEIVING PARTY")
    path = Path(folder) / "two_usual_roles.pdf"
    doc.save(path)
    return str(path)


def client_signed_visibly(folder, blocks=2, signed=1):
    """A mutual NDA whose blocks name nobody ('Signature: ____'); the client's ink is already on some of them."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "MUTUAL NON-DISCLOSURE AGREEMENT", fontsize=16, fontname="hebo")
    paragraph(page, 100, "This Mutual Non-Disclosure Agreement is made between the parties who sign below.")
    ink = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 30), False)
    ink.clear_with(40)
    for i in range(blocks):
        y = 260 + i * 90
        page.insert_text((72, y), "Signature:", fontsize=11)
        page.draw_line((140, y + 1), (330, y + 1), color=(0, 0, 0), width=0.8)
        page.insert_text((72, y + 26), "Date:", fontsize=11)
        page.draw_line((140, y + 27), (260, y + 27), color=(0, 0, 0), width=0.8)
        if i < signed:
            page.insert_image(pymupdf.Rect(150, y - 22, 270, y), pixmap=ink)    # the client's signature
    path = Path(folder) / f"client_signed_visibly_{blocks}_{signed}.pdf"
    doc.save(path)
    return str(path)


def two_party_doc(folder, name, preamble, left_header, right_header):
    """A one-page contract: a preamble, then two side-by-side signature blocks."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "AGREEMENT", fontsize=16, fontname="hebo")
    y = 100
    for part in preamble.split("\n"):
        y = paragraph(page, y, part)
    signature_rows(page, 72, 400, left_header)
    signature_rows(page, 330, 400, right_header)
    path = Path(folder) / name
    doc.save(path)
    return str(path)


# =============================================================================
# Values and helpers shared by the tests
# =============================================================================

SETTINGS = dict(storage.DEFAULT_SETTINGS)
ASPECT = 3.3     # width / height of a typical signature image
# The app reads the signature from settings["signature_file"] (data/signature.png) under AUTOSIGN_HOME.
TEST_SIGNATURE = storage.HOME / SETTINGS["signature_file"]


def spot(analysis, spot_id):
    return next(s for s in analysis["spots"] if s["id"] == spot_id)


def ours(analysis):
    return [s for s in analysis["spots"] if s["owner"] == "ours"]


def agreeing_ai(analysis, settings=SETTINGS):
    """A stand-in AI answer that agrees with the rules (used where the real model isn't the point)."""
    picks = [{"signature_spot": s["id"], "date_spot": s["date_id"] or 0, "quote": s["block_text"] or s["label"]}
             for s in ours(analysis)]
    return {"ok": True, "digest": "test", "answer": {"document_type": analysis["doc_type"],
            "our_role": (analysis["parties"] or {}).get("our_role") or "unknown", "picks": picks}}


def ai_answer(analysis, picks):
    """A stand-in AI answer with the given picks: [(spot, date, quote), ...]."""
    return {"ok": True, "answer": {"document_type": analysis["doc_type"], "our_role": "unknown",
                                   "picks": [{"signature_spot": s, "date_spot": d, "quote": q} for s, d, q in picks]}}


SAMPLES = {
    # file: (page with the signature spots, number of spots, document type)
    "Service-Contract-Template.pdf": (4, 2, "service_agreement"),
    "Mutual-NDA-Template-QuickSigner.pdf": (5, 2, "mutual_nda"),
    "Employment-Contract-Agreement.pdf": (6, 2, "employment"),
    "llc-operating-agreement-template.pdf": (15, 4, "operating_agreement"),
    "LLC-Operating-Agreement-Template-QuickSigner.pdf": (6, 2, "operating_agreement"),
}


def png_signature():
    return pipeline.prepare_signature(TEST_SIGNATURE.read_bytes())


def sign_proposal(pdf, analysis, out, flatten=False):
    """Sign every block the rules say is yours. Returns (result, first placement)."""
    date_text = "Nov 10, 2026"
    png = png_signature()
    aspect = pipeline.image_aspect(png)[0]
    choices = [{"spot": s["id"], "date": s["date_id"]} for s in ours(analysis)]
    placements = pipeline.placements_for_spots(analysis, choices, pdf, aspect, date_text)
    result = pipeline.sign_pdf(pdf, str(out), placements, png, date_text, analysis["mode"], flatten, "Test Signer")
    return result, placements[0]


def set_auto_sign(client, on):
    response = client.put("/api/settings", json={"auto_sign": on})
    assert response.status_code == 200, response.text


def upload(client, path, name=None):
    data = Path(path).read_bytes()
    response = client.post("/api/documents", files=[("files", (name or Path(path).name, data, "application/pdf"))],
                           data={"source": "gui"})
    assert response.status_code == 200, response.text
    return response.json()["jobs"][0]


def wait_for(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "processing"):
            return job
        time.sleep(0.2)
    raise AssertionError(f"Job {job_id} is still {job['status']}")
