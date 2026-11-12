"""
The signing policy: every word list, distance and limit the rules use, in one place.

Change a value here to tune the rules; the tests in tests/ show what each one does.
"""

RULES_VERSION = "rules_v1"
PROMPT_VERSION = "judge_v2"          # v2: the AI lists every block that is yours, not just one
VERIFY_PROMPT_VERSION = "verify_v1"


MIN_LINE_WIDTH = 45          # pt. Shorter rules are not signature or date lines.
# pt. Stamp heights to try, tallest first (0.44 in down to 0.25 in). Wide signatures need the
# lower heights to fit along a 2-inch line: a 7:1 signature at 20 pt is 140 pt long.
SIGNATURE_HEIGHTS = [32, 29, 26, 23, 20, 18]
DROP_RATIOS = [0.15, 0.08, 0.0]    # Share of the stamp height that sits below the line.
DATE_FONT_SIZE = 10
ROW_TOLERANCE = 3.5          # pt. Words whose bottoms are this close share a row.
SEGMENT_GAP = 12             # pt. A wider gap splits a row into separate labels.
HEADER_REACH = 70            # pt above a spot to look for its header ("PARTY A").
BLOCK_REACH = 100            # pt below a spot to look for Name, Title and Date rows.
MAX_LABEL_WORDS = 6          # A signature label is short: "Service Provider's Signature".

# Words that make a short label a signature label.
SIGN_WORDS = {"signature", "signed", "sign", "signatory", "signer"}
# Blocks we must never sign.
NEVER_WORDS = {"witness", "witnessed", "notary", "notarial", "guarantor", "surety",
               "attest", "attested", "attestation"}
# Words that show a "signature" label is not a place to sign.
NOT_A_SPOT_WORDS = {"page", "pages", "follows", "follow", "initial", "initials", "initialed",
                    "electronic", "electronically", "esign", "counterpart", "counterparts",
                    "whereof", "block", "blocks"}
# "Effective Date", "Start Date"... are not the signing date.
NOT_SIGNING_DATE_WORDS = {"effective", "commencement", "start", "end", "termination", "expiration",
                          "expiry", "birth", "due", "delivery", "completion", "renewal", "closing", "issue"}
# Rows that are fields inside a signature block (they belong to the block above them).
FIELD_WORDS = {"name", "print", "printed", "title", "its", "date", "dated", "address", "email",
               "mail", "phone", "telephone", "tel", "fax", "position", "designation", "capacity",
               "signature", "by"}
# "Company: Acme Inc." rows: a field when a value follows, a header when alone.
ENTITY_WORDS = {"company", "entity", "organization", "organisation"}

# Roles a party can have. The settings add the company's usual roles.
KNOWN_ROLES = ["service provider", "provider", "client", "customer", "company", "consultant",
               "contractor", "independent contractor", "vendor", "supplier", "disclosing party",
               "receiving party", "discloser", "recipient", "party a", "party b", "party 1",
               "party 2", "first party", "second party", "employer", "employee", "licensor",
               "licensee", "buyer", "seller", "purchaser", "member", "manager", "landlord",
               "tenant", "lessor", "lessee", "agency", "agent", "partner", "owner", "lender",
               "borrower", "subcontractor", "investor", "distributor", "reseller", "sponsor",
               "principal"]

# Document types: the earliest match on page 1 wins (the title comes first).
DOC_TYPE_PATTERNS = [
    ("mutual_nda", r"mutual\s+(non[\s-]?disclosure|confidentiality)"),
    ("nda", r"non[\s-]?disclosure|confidentiality\s+agreement|\bnda\b"),
    ("msa", r"master\s+services?\s+agreement"),
    ("sow", r"statement\s+of\s+work"),
    ("service_agreement", r"services?\s+(agreement|contract)|consult\w*\s+agreement|professional\s+services"),
    ("employment", r"employment\s+(agreement|contract)"),
    ("operating_agreement", r"operating\s+agreement"),
    ("lease", r"\blease\b|rental\s+agreement"),
]
DOC_TYPES = ["nda", "mutual_nda", "service_agreement", "msa", "sow", "employment",
             "operating_agreement", "lease", "other"]
DOC_TYPE_FAMILY = {"nda": "nda", "mutual_nda": "nda", "service_agreement": "services",
                   "msa": "services", "sow": "services"}

# Text that looks like instructions to an AI model (prompt injection).
INSTRUCTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier|preceding) (instructions|prompts?|rules)",
    r"disregard (all |any |the )?(previous|prior|above|earlier) ",
    r"\b(you are|you're) (now )?(an? )?(ai|assistant|language model|llm|chatbot)\b",
    r"\bas an ai\b",
    r"\bsystem prompt\b",
    r"\b(sign|stamp) (every|all|each|any) (box|block|line|field|spot|page|signature)",
    r"\b(choose|select|pick|answer) (spot|candidate|block|box|option) (number |no\.? |#)?\d",
    r"\bassistant:",
]
# Contract clauses that say how it must be signed. These always go to a person.
EXECUTION_PATTERNS = [
    r"wet[\s-]ink", r"\bnotari[sz]ed\b", r"notary public", r"executed as a deed",
    r"(signed|executed|delivered)[^.]{0,40}in the presence of", r"in the presence of (a |the )?witness",
    r"(via|through|using) docusign", r"adobe sign",
    r"original (ink )?signatures? (is|are) required", r"signed in ink",
]
# Not listed on purpose: "under seal". Most US NDAs carry the trade-secret whistleblower notice
# ("...if such filing is made under seal"), which says nothing about how to sign.


LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
                  "plc", "gmbh", "ag", "sa", "bv", "pty", "lp", "llp"}


EVIDENCE_TEXT = {"name": "your name is in the block", "role": "your role, defined next to your name",
                 "unnamed_party": "the only party left unnamed", "usual_role": "one of your usual roles",
                 "empty_block": "still empty after the other party signed",
                 "page_break": "named only at the bottom of the previous page"}
STRONG_EVIDENCE = {"name", "role"}
# Accepted when 'Require your company name' is off. 'page_break' is never enough to auto-sign.
NAMELESS_EVIDENCE = STRONG_EVIDENCE | {"unnamed_party", "usual_role", "empty_block"}
