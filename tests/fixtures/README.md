# kaos-pdf — fixture provenance

Per the monorepo fixture provenance policy
([`kaos-modules/docs/oss/50-data-and-fixtures/provenance-policy.md`](https://github.com/273v/kaos-modules/blob/main/docs/oss/50-data-and-fixtures/provenance-policy.md)),
every fixture in this directory must answer: where did this file come from,
what is its license, when did we retrieve it, and what is its content hash.

All 17 fixtures tracked here are **public-domain U.S. government work product**
(17 USC §105) or **non-precedential state court orders** released through public
filing systems, with one **intentionally malformed PDF** for negative-path
testing. None of these files originated from a customer engagement; none are
privileged. Seven of them (`test1.pdf`–`test6.pdf`, `bad_test1.pdf`) were
inherited verbatim from the predecessor `kelvin_pdf/tests/resources/` corpus
(SHA-256 confirmed identical against
`/home/mjbommar/projects/273v/kelvin-modules/kelvin_pdf/tests/resources/`); they
were added there in commit `fb11491` ("kelvin pdf 2.0.7") and ported to KAOS in
commit `82ccafd`.

The `stress/` subdirectory is **gitignored** and not covered by this README;
see the monorepo's
[`kaos-modules/docs/oss/50-data-and-fixtures/pii-and-customer-scan.md`](https://github.com/273v/kaos-modules/blob/main/docs/oss/50-data-and-fixtures/pii-and-customer-scan.md)
for the cleanup plan against any pattern-matched commercial documents left in
working trees.

## Manifest

| File | Source | License | Retrieved | SHA-256 | Notes |
|---|---|---|---|---|---|
| `bad_test1.pdf` | Hand-crafted negative fixture — valid PDF 1.2 header + linearization dict, payload zeroed out so PDFium reports `Data format error`. Inherited from `kelvin_pdf/tests/resources/bad_test1.pdf` (commit `fb11491`). | hand-crafted, 273V | 2026-03-27 | `5fd2e952d1379c78752744cd6aaba17d46d37713d019f4ef27b40e9f768dbbdb` | Drives error-path tests in `tests/fuzz/test_fuzz.py` and is explicitly excluded from corpus/stress runs. |
| `casd_court_order.pdf` | U.S. District Court for the Southern District of California — "Judgment in a Criminal Case for Revocations" (AO 245D), scanned and OCR'd via Adobe Acrobat 9.45 Paper Capture Plug-in. PACER-style criminal-revocation order. | public-domain (17 USC §105) | 2026-03-28 | `cac5be3464c6c8bf4bc567a95985996af544d3cd8f659fce488d3c4e5fc2bb7d` | Tests scanned-PDF + OCR-layer path. Producer string: "Adobe Acrobat 9.45 Paper Capture Plug-in; modified using iText 2.1.7 by 1T3XT". |
| `gpo_report.pdf` | U.S. Department of Agriculture, *Northeastern Loggers' Handbook*, Agriculture Handbook No. 6 (1951), digitized by GPO / National Agricultural Library. Producer "LuraDocument PDF Compressor Server" indicates GPO digitization workflow. | public-domain (17 USC §105) | 2026-03-28 | `89dc74990b3725b3891a6cd11878e5bbb48bce8f3c52e8b9fbe348fbf6e241bd` | 168-page large-corpus fixture (7.6 MB) — used for benchmarking and memory-pressure tests. |
| `kl3m_court_burns.pdf` | *Burns v. Fahrner*, No. 733 EDA 2022, Superior Court of Pennsylvania (non-precedential decision, J-A21035-22). Pulled from the KL3M reference corpus (273V research dataset of public court opinions). | public-domain (state court order, public docket) | 2026-03-27 | `53d8fbb2e6fe6f612db1cb4c47b0f1831d9c645f2a436795d3858d9a643a64d0` | 31 pages. Producer string flags AGPL-version iTextSharp on upstream conversion — not redistributed. |
| `kl3m_court_woods.pdf` | *Ex parte Alvin Charles Woods*, No. WR-75,814-04, Texas Court of Criminal Appeals (Keller, P.J., concurring). Pulled from the KL3M reference corpus. | public-domain (state court order, public docket) | 2026-03-27 | `3778c1ec6e7c083374844cc576c387626c1ab8b3d640a3eeb1a245c6fe3dcb27` | 8 pages, native text. |
| `kl3m_dot_attachment.pdf` | Public comment attached to U.S. DOT Docket OST-2008-0299 ("Essential Air Service at El Centro/Imperial, California") — letter from the City of Imperial, CA City Council dated 2010-11-04. Filed publicly on regulations.gov / DOT docket. | public-domain (public docket filing) | 2026-03-27 | `a522cccdd368ceaf4a0d33943a724a0ea4054938d73562a5c0cbaa088a34f5a0` | 1 page, OCR-scanned letter on city letterhead. |
| `kl3m_fda_guidance.pdf` | *Federal Register* Vol. 76, No. 20 (Monday, January 31, 2011) — FDA notice on Affordable Care Act §4205 vending-machine operator registration estimate (page 5386). Producer: "Federal Digital System, U. S. Government Printing Office". | public-domain (17 USC §105) | 2026-03-27 | `83299311310f37608a1fe5f0518424558e0d3faf3987121f9db4de342724c216` | 2 pages, GovInfo (FDsys) export. |
| `ornamental_plaster.pdf` | National Park Service, *Preservation Briefs* — "Preserving Historic Ornamental Plaster" by David Flaharty. U.S. Department of the Interior, Cultural Resources / Heritage Preservation Services. | public-domain (17 USC §105) | 2026-03-28 | `333e6ed239f20a51a59e464a1fe5329ee2cd7380399d80fdb23dcf10c08ccdb3` | 14 pages, 10 MB — heavily-imaged scanned brief; used for image-extraction tests. |
| `staten_v_united_states.pdf` | *Staten v. United States*, No. 15-308C (Fed. Cl. July 17, 2015) — U.S. Court of Federal Claims order on motion to dismiss. Not-for-publication order from a federal court of record. | public-domain (17 USC §105) | 2026-03-28 | `a627b0fcfab942b97e90aae4f95f3392448fcfde9ed4a914dffd45efaaca8d40` | 7 pages, scanned via Canon device. |
| `test1.pdf` | *Federal Register* notice page (Proposed Rules header). Producer: "iText® Core 7.2.3 ... Government Publishing Office", Creator: "govinfo, U. S. Government Publishing Office", CreationDate 2024-08-24. GovInfo export. Inherited from `kelvin_pdf/tests/resources/test1.pdf`. | public-domain (17 USC §105) | 2026-03-27 | `a3d0bb7439edf4ef452263451e81328221937186bfa7b725c7b64fd38270d7b4` | 1 page, native text. |
| `test2.pdf` | Public Law 92-500, Oct. 18, 1972 — Federal Water Pollution Control Act Amendments of 1972 (Clean Water Act enactment), 86 Stat. 816 et seq. Statutes-at-Large excerpt. Inherited from `kelvin_pdf/tests/resources/test2.pdf`. | public-domain (17 USC §105) | 2026-03-27 | `8a692403d29fa72cb27e68a082943363778ff66c91b4adc2a086c0d73438c20e` | 1 page. |
| `test3.pdf` | Scanned PDF, 32 pages, Producer "Xerox WCP C2636" (Xerox WorkCentre Pro scanner), CreationDate 2009-06-18. Native text layer is empty — image-only scan used to drive the OCR / image-extraction path. Inherited from `kelvin_pdf/tests/resources/test3.pdf`; upstream source not recorded in kelvin history. | `<unknown>` (TODO: identify upstream document; in-tree behavior treats it as opaque scanned input) | 2026-03-27 | `b4eaee56996a4c7b5ae5be5f94feb83d84937f318f0e6bcd16629ea344cc4ea5` | TODO: re-derive source from upstream kelvin commit history or replace with a known-source scanned fixture. Until then, treat as opaque image-only PDF for OCR-path tests only. |
| `test4.pdf` | Code of Federal Regulations excerpt — 17 CFR §31.3 (Commodity Futures Trading Commission, leverage transactions / governing-law provisions). Producer: GovInfo iText pipeline, CreationDate 2024-01-17. Inherited from `kelvin_pdf/tests/resources/test4.pdf`. | public-domain (17 USC §105) | 2026-03-27 | `7ffc62c37e2731f7d14e1ad689e8be9e2c787c00b1c316ffa714b3107cf4956a` | 34 pages. |
| `test5.pdf` | NHTSA / U.S. DOT, *Motor Vehicle Defects & Recalls — What Every Vehicle Owner Should Know* (booklet, DOT publication). Producer: "Adobe PDF Library 7.0", Creator: "Adobe InDesign CS2", CreationDate 2006-05-10. Inherited from `kelvin_pdf/tests/resources/test5.pdf`; the corresponding plain-text version lives at `kelvin_pdf/tests/resources/test5.txt`. | public-domain (17 USC §105) | 2026-03-27 | `b669a0eb67ed82f2b9d219cdead903a0ce72af44242bbbd9ff4314fcdbfe8acb` | 20 pages, mostly image-based layout (image-only on pages 0–1; native text begins page 2). |
| `test6.pdf` | *Federal Register* Vol. 79, No. 131 (Wednesday, July 9, 2014) — Notices section, page 38901 (AHRQ Hospital Informed Consent burden estimate). Producer: "Federal Digital System, U. S. Government Printing Office". Inherited from `kelvin_pdf/tests/resources/test6.pdf`. | public-domain (17 USC §105) | 2026-03-27 | `da51af663fd8227b354abf25018b615e5639ba61ec167a96ee58bb1edf1e52a2` | 3 pages, native text. |
| `usgs_sir.pdf` | USGS Scientific Investigations Report 2006-5154 — *Estimated Water Use and Availability in the Pawtuxet and Quinebaug River Basins, Rhode Island, 1995–99*, by Emily C. Wild and Mark T. Nimiroski. Prepared in cooperation with the Rhode Island Water Resources Board. Canonical URL: <https://pubs.usgs.gov/sir/2006/5154/>. | public-domain (17 USC §105) | 2026-03-28 | `261b4fb97aca6c9e5540c80b11fe43fab56fda92ea5baa536c4f50c71bca0ad8` | 76 pages, 4.3 MB. Used as a long-form, figure-rich benchmark fixture. |

## Confirmed-source vs. `<unknown>` count

- Confirmed source: **16 / 17**
- `<unknown>` with TODO: **1 / 17** (`test3.pdf` — opaque image-only scan inherited from `kelvin_pdf`; upstream-source recovery is the only outstanding provenance task in this directory)

## Audit confirmations

1. No file in this directory is a customer document, a pseudonymized client
   document, or a privileged communication. All confirmed-source files are U.S.
   federal work product, state court orders from public dockets, or
   hand-crafted negative fixtures.
2. None of these files trigger the denylist in the monorepo's
   [`kaos-modules/docs/oss/10-licensing-legal/dep-license-policy.md`](https://github.com/273v/kaos-modules/blob/main/docs/oss/10-licensing-legal/dep-license-policy.md)
   (no CC-BY-NC, no SSPL, no
   AGPL on the file content itself — note that `kl3m_court_burns.pdf`'s Producer
   string mentions an AGPL build of iTextSharp used by the *upstream* converter,
   which does not affect the public-domain status of the underlying court
   order).
3. SHA-256 values can be regenerated with
   `sha256sum tests/fixtures/*.pdf`.

## Refreshing this manifest

If a fixture is added, replaced, or rebuilt:

1. Recompute the SHA-256 with `sha256sum <file>`.
2. Update the row in this table — including the `Retrieved` date.
3. Add the canonical upstream URL when one is available.
4. If the fixture replaces a `<unknown>` entry, also clear the TODO note.
