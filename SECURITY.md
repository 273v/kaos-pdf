# Security policy

## Reporting a vulnerability

We take security seriously. If you believe you have found a security
vulnerability in `kaos-pdf`, please report it privately so we can address it
before public disclosure.

**Please do not file a public GitHub issue for security reports.**

### How to report

Use [GitHub Private Vulnerability Reporting](https://github.com/273v/kaos-pdf/security/advisories/new)
to send a report. Alternatively, email **security@273ventures.com**.

Include as much of the following as you can:

- A description of the vulnerability and its impact
- Steps to reproduce, including affected versions
- Any proof-of-concept code, if available
- Suggested mitigations, if you have any

### What to expect

- **Acknowledgement** — within 3 business days of your report.
- **Initial triage** — within 7 business days, including a severity assessment.
- **Fix and disclosure** — coordinated with you. Our target window is 90 days
  from acknowledgement to public disclosure, faster for high-severity issues.
- **Credit** — we credit reporters in the release notes and security advisory
  unless you prefer to remain anonymous.

## Supported versions

`kaos-pdf` follows Semantic Versioning. While the project is pre-1.0, only
the latest minor release receives security fixes. After 1.0, the latest two
minor releases will be supported.

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |
| < 0.1   | No        |

## Scope

`kaos-pdf` extracts text, layout, tables, metadata, outline, and
rendered page images from PDF documents using ``pypdfium2`` (Apache-2.0
PDFium bindings). Optional OCR via Tesseract; optional table extraction
via Camelot or Tabula. Output is a ``kaos-content`` ``ContentDocument``
AST plus typed metadata models. Tools are exposed via ``register_pdf_tools(runtime)``
and consumed by ``kaos-mcp`` over MCP.

In-scope:

- The `kaos-pdf` Python package as published on PyPI
- The `273v/kaos-pdf` GitHub repository (CI, release, supply chain)
- PDF input handling — malformed / encrypted / oversize files, deeply
  nested page trees, malicious metadata, font-rendering paths inherited
  from PDFium
- Tool boundary (`ParsePDFTool`, `GetPageTextTool`, `RenderPageTool`,
  `PDFMetadataTool`, `SearchDocumentTool`, `GetOutlineTool`,
  `ClassifyPageTool`) — input validation, response shaping, tool
  annotation correctness (`readOnlyHint`, `idempotentHint`)
- Thread-safety of the global PDFium lock (see `docs/THREAD_SAFETY.md`);
  multi-thread / multi-process callers must not corrupt PDFium state
- OCR engine wrapper (`TesseractEngine`) — subprocess invocation,
  argument quoting, image input handling
- Table extraction engine wrappers (Camelot / Tabula) — same surface
- OIDC trusted-publishing release pipeline

Out of scope:

- Vulnerabilities in third-party dependencies — report upstream
  (`pypdfium2`, `pillow`, `pytesseract`, `camelot-py`, `tabula-py`,
  `pydantic`, `kaos-core`, `kaos-content`).
- The bundled PDFium binary itself — Foxit / Google issue, report to
  the PDFium project.
- Tesseract, Camelot, or Tabula crashes on adversarial input — report
  to the upstream tool; we wrap them but cannot fix their parsers.
- MCP transport security — that surface lives in `kaos-mcp`; report
  there.
- Issues caused by user-supplied configuration that explicitly disables
  safety features (e.g. raising `max_resource_bytes` past the published
  defaults, or running OCR on attacker-controlled images without an
  allowlist).
