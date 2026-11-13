"""
Every word list, distance and limit the rules use, in one place.

Change a value here to tune the rules; the tests in tests/ show what each one does.
Distances are PDF points (72 pt = 1 inch).
"""

RULES_VERSION = "rules_v1"
PROMPT_VERSION = "client_judge_v2"      # v2: the AI also lists the company's own signature spots


# =============================================================================
# Lines, labels and stamps
# =============================================================================

MIN_LINE_WIDTH = 45          # Shorter rules are not signature or date lines.
# Stamp heights to try, tallest first (0.44 in down to 0.25 in). Wide signatures need the lower
# heights to fit along a 2-inch line: a 7:1 signature at 20 pt is 140 pt long.
SIGNATURE_HEIGHTS = [32, 29, 26, 23, 20, 18]
DROP_RATIOS = [0.15, 0.08, 0.0]    # Share of the stamp height that may sit below the line.
DATE_FONT_SIZE = 10
ROW_TOLERANCE = 3.5          # Words whose bottoms are this close share a row.
SEGMENT_GAP = 12             # A wider gap splits a row into separate labels.
HEADER_REACH = 70            # How far above a spot to look for its header ("PARTY A").
BLOCK_REACH = 100            # How far below a spot to look for its Name, Title and Date rows.
MAX_LABEL_WORDS = 6          # A signature label is short: "Service Provider's Signature".


# =============================================================================
# Words
# =============================================================================

# Words that make a short label a signature label.
SIGN_WORDS = {"signature", "signed", "sign", "signatory", "signer"}
# Blocks nobody signs through AutoSign.
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
# The endings of company names ("Acme Analytics Inc.").
LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
                  "plc", "gmbh", "ag", "sa", "bv", "pty", "lp", "llp"}


# =============================================================================
# Roles and document types
# =============================================================================

# Roles a party can have in a contract.
KNOWN_ROLES = ["service provider", "provider", "client", "customer", "company", "consultant",
               "contractor", "independent contractor", "vendor", "supplier", "disclosing party",
               "receiving party", "discloser", "recipient", "party a", "party b", "party 1",
               "party 2", "first party", "second party", "employer", "employee", "licensor",
               "licensee", "buyer", "seller", "purchaser", "member", "manager", "landlord",
               "tenant", "lessor", "lessee", "agency", "agent", "partner", "owner", "lender",
               "borrower", "subcontractor", "investor", "distributor", "reseller", "sponsor",
               "principal"]

# Roles a client usually has in the agreement a company sends it. A block that names one of these
# is the client's (unless the contract gives that role to the sending company).
CLIENT_ROLES = ["Client", "Customer", "Patient", "Member", "Participant", "Buyer", "Purchaser", "Tenant",
                "Lessee", "Borrower", "Licensee", "Policyholder", "Insured", "Claimant", "Applicant",
                "Student", "Subscriber", "Homeowner"]

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


# =============================================================================
# Red flags: text that means a person must look at the document
# =============================================================================

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
# Contract clauses that say how it must be signed.
EXECUTION_PATTERNS = [
    r"wet[\s-]ink", r"\bnotari[sz]ed\b", r"notary public", r"executed as a deed",
    r"(signed|executed|delivered)[^.]{0,40}in the presence of", r"in the presence of (a |the )?witness",
    r"(via|through|using) docusign", r"adobe sign",
    r"original (ink )?signatures? (is|are) required", r"signed in ink",
]
# Not listed on purpose: "under seal". Most US NDAs carry the trade-secret whistleblower notice
# ("...if such filing is made under seal"), which says nothing about how to sign.


# =============================================================================
# AutoSign
# =============================================================================

DATE_FORMAT = "%b %d, %Y"         # zero-padded day, so every date has the same length
SIGNATURE_ASPECT = 3.5            # width / height of every stamped signature (each image is padded to it)
MAX_PAGES = 100                   # bigger agreements are refused
MAX_MB = 25
MAX_SIGNATURE_BYTES = 2_000_000   # the signature image sent by the browser
