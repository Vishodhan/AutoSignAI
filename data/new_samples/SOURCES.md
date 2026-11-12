# Additional test PDFs: sources and signature shapes

Downloaded 10 Nov 2026. Each file was checked with PyMuPDF: it is a real PDF, and the
page numbers, form fields and signature shapes below were checked against the file.
"/Sig fields" means real AcroForm signature fields (`/FT /Sig`). Your first 5 samples have none.

| # | File | Pages / size | Signs on | Signature shape | What it adds to the test set | License |
|---|------|--------------|----------|-----------------|------------------------|---------|
| 1 | commonpaper-mutual-nda.pdf | 2 / Letter | p1 (cover page) | Two-column table: PARTY 1 and PARTY 2 columns, with row labels (Signature, Print Name, Title, Company, Date) on the left. 2 /Sig fields | Signature on page 1, with the terms after it. The column decides who owns the block | CC BY 4.0 |
| 2 | bonterms-mutual-nda-terms.pdf | 1 / Letter | none | Standard terms only. No signature block | Negative case: must report "no block found" | CC BY 4.0 |
| 3 | govuk-mutual-nda.pdf | 2 / A4 | p2 | "Signed [by …] OR [on behalf of …] by its duly authorised representative:", then a line with the label under it (Signature / Name / Position), shown twice | A4 page size, UK wording ("Position"), party names in brackets as placeholders | OGL v3.0 |
| 4 | national-archives-nda.pdf | 2 / A4 | p2 | Full-width rule with "(Signature, on behalf of …)" under it | A4. The line runs the full page width | OGL v3.0 |
| 5 | ma-standard-contract.pdf | 2 / Letter | p2, bottom | Bordered cells side by side, under shaded headers: "AUTHORIZING SIGNATURE FOR THE CONTRACTOR" and "… FOR THE DEPARTMENT". 2 /Sig fields | Signature spot is a form cell, not a rule. The label is a header bar above it | MA public form |
| 6 | gsa-sf1449.pdf | 2 / Letter | p1 cells 30a/31a; p2 receiving cells | Grid of numbered cells. 4 /Sig fields. Reader-extended (`/Perms /UR3`) | Numbered-cell grid, with one cell for the offeror and one for the government. **The UR3 usage-rights signature has a `/ByteRange` but is not a person's signature** | US Gov, public domain |
| 7 | gsa-sf30.pdf | 2 / Letter | p1 cells 15B/16B | Cells side by side, with "(Signature of person authorized to sign)" under the rule. 2 /Sig fields. UR3 | Contractor on the left, contracting officer on the right | US Gov, public domain |
| 8 | fdp-fixed-amount-subaward.pdf | 10 / Letter | p1, bottom | Bordered two-column table: "By an Authorized Official of Pass-through Entity" and "… of Subrecipient", with Name/Title/Date under each. About 200 form fields, no /Sig field | Signature on the first page of a 10-page agreement. Attachments mention "authorized official" again | FDP template, free to use |
| 9 | ubmta-implementing-letter.pdf | 2 / Letter | p2 | Three stacked blocks: Provider Scientist, Recipient Scientist, Recipient Organization Certification. Label on the left of each line. 3 /Sig fields | 3 to 4 signer roles. The person and the organization sign separately | Cornell copy of the federal UBMTA form |
| 10 | michigan-statutory-poa.pdf | 6 / Letter | p4 (principal + 2 witnesses), p5–6 | Stacked blocks, each label under its line. 3 /Sig fields. Notary block | Witness and notary blocks that must **not** be signed | State of Michigan |
| 11 | georgia-statutory-poa.pdf | 8 / Letter | p4 principal, p5 witnesses, p6 notary, p7 agent | "Your signature / Date" lines. Initial blanks on pp2–3 | Signing spread over 4 pages, plus initials | Statutory form (Emory Law copy) |
| 12 | hud-model-lease.pdf | 22 / Letter | p21–22 (Sec. 44) | Lines with labels under them (Witness, Landlord, Tenant ×4) in the lower half of the page. **The last Tenant line runs onto p22** | Block split across a page break. The signing page never uses the word "Signature" | US Gov, public domain |
| 13 | flbar-single-family-lease.pdf | 10 / Letter | p5, p8, p10 | "Landlord's Signature / Date" with the label under the line, repeated. "(initial) ____" blanks on pp4–7 | Several signing places, one of them in an addendum, plus initial spots | Florida Bar ©, internal testing only |
| 14 | irs-w9.pdf | 6 / Letter | p1, middle of page | "Sign Here" label in a bordered row. No /Sig field. UR3 | Signature in the middle of the page, on a form rather than a contract | US Gov, public domain |
| 15 | upenn-ic-agreement.pdf | 4 / Letter | p3 | "Signature: ____ Date: ____" with the label on the left. Party header "[SERVICE PROVIDER]" is a highlighted placeholder. 2 /Sig fields | Party name is a bracketed placeholder | UPenn ©, internal testing only |
| 16 | cuny-ic-agreement.pdf | 18 / Letter | p1 (+ p2 notary, p13 exhibit) | By:/Name:/Title: in two columns (CONTRACTOR, THE CITY UNIVERSITY OF NEW YORK) right after IN WITNESS WHEREOF, plus "prepared by" lines | Signature on p1 of 18. The exhibit has its own signature lines. p18 is instructions that mention signatures (decoy) | CUNY ©, internal testing only |
| 17 | wpunj-ic-agreement.pdf | 4 / Letter | p4 | Label-left lines: two university signers stacked, then the contractor | Two signers for the same party | WPUNJ ©, internal testing only |
| 18 | xavier-ic-form.pdf | 2 / Letter | p1, bottom | Excel-exported table with small cells. 4 /Sig fields, including "VP Signature" and "Completed Confirm" | Dense table. Internal approval signatures that belong to neither contract party | Xavier ©, internal testing only |
| 19 | dss-pades-5-signatures.pdf | 1 / A4 | already signed | 5 PAdES signatures + 1 document timestamp, invisible fields | Tests the "client already signed" path. Any change must be an incremental save | esig/dss test file (LGPL-2.1 repo) |

## Download URLs

1. https://commonpaper.com/wp-content/uploads/2021/12/Mutual-NDA-public-2page.pdf
2. https://raw.githubusercontent.com/Bonterms/Mutual-NDA/main/Bonterms-Mutual-NDA-Version-1.pdf
3. https://assets.publishing.service.gov.uk/media/5a8188fd40f0b62302697d5e/Example-Mutual-Non-Disclosure-Agreement.pdf
4. https://cdn.nationalarchives.gov.uk/documents/non-disclosure-agreement-template.pdf
5. https://www.macomptroller.org/wp-content/uploads/form_standard-contract.pdf
6. https://www.gsa.gov/system/files/SF1449-21.pdf
7. https://www.gsa.gov/cdnstatic/SF30-16c.pdf
8. https://thefdp.org/wp-content/uploads/FIXEDA1.pdf
9. https://ctl.cornell.edu/wp-content/uploads/forms/UBMTA-Implementing-Letter.pdf
10. https://www.michigan.gov/ag/-/media/Project/Websites/AG/EATF/Michigan-Statutory-Form-Power-of-Attorney.pdf
11. https://law.emory.edu/_includes/documents/sections/academics/blank-statutory-form-power-of-attorney-for-georgia-fillable.pdf
12. https://www.hud.gov/sites/dfiles/OCHCO/documents/90105a.pdf
13. https://www-media.floridabar.org/uploads/2018/08/single-family-lease.pdf
14. https://www.irs.gov/pub/irs-pdf/fw9.pdf
15. https://www.finance.upenn.edu/wp-content/uploads/Global-IC-Agreement-Template-021726-form.pdf
16. https://www.journalism.cuny.edu/wp-content/uploads/2020/02/CUNY-Independent-Contractor-Agreement.pdf
17. https://wpconnect.wpunj.edu/emps/eforms/accounts_payable/Independent%20Contractor%20Agreement%20Template%20V1.pdf
18. https://www.xavier.edu/pdf/recreational-sports/independent-contractor-form.pdf
19. https://raw.githubusercontent.com/esig/dss/master/dss-pades/src/test/resources/validation/pades-5-signatures-and-1-document-timestamp.pdf

Not downloaded: DD Form 1155 (esd.whs.mil returned HTTP 403 to scripted downloads; get it in a
browser) and the FDP Foreign Fixed Amount Subaward (almost the same as #8).
