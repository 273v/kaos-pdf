# kaos-pdf Quickstart

## Installation

```bash
pip install kaos-pdf

# With MCP server support (for Claude Code / Claude Desktop):
pip install 'kaos-pdf[mcp]'
```

## CLI Usage

### Extract PDF to Markdown

```bash
kaos-pdf extract document.pdf                          # → stdout
kaos-pdf extract document.pdf --output doc.md          # → file
kaos-pdf extract document.pdf --format text            # → plain text
kaos-pdf extract document.pdf --format json            # → JSON AST
kaos-pdf extract document.pdf --pages 1-5              # → first 5 pages
kaos-pdf extract document.pdf --pages 1,3,7-10         # → specific pages
```

### Search Within a PDF

```bash
kaos-pdf search contract.pdf "indemnification"
kaos-pdf search filing.pdf "material adverse change" --top-k 5
```

Output:
```
Results for "indemnification" (3 matches):

[1] Score: 4.0 | Page 12 | Liability
    The Contractor shall indemnify, defend, and hold harmless...

[2] Score: 2.0 | Page 18 | General Provisions
    Subject to the indemnification obligations set forth...
```

### PDF Info

```bash
kaos-pdf info report.pdf
```

Output:
```
File:     report.pdf
Pages:    75
Type:     text
Title:    Estimated Water Use and Availability
Author:   Emily C. Wild
Producer: Acrobat Distiller 7.0.5
Outline:  13 entries
```

### Outline / Table of Contents

```bash
kaos-pdf outline report.pdf
```

Shows PDF bookmarks if available, otherwise detected headings.

### Single Page Text

```bash
kaos-pdf page report.pdf 5    # page 5 (1-based)
```

## Python API

### Extract and Convert

```python
from kaos_pdf import extract_pdf
from kaos_content import serialize_markdown

doc = extract_pdf("contract.pdf")
print(serialize_markdown(doc))
```

### Search

```python
from kaos_pdf import extract_pdf, search_document

doc = extract_pdf("contract.pdf")
for r in search_document(doc, "indemnification", top_k=5):
    print(f"p{r.page} | {r.section_title} | {r.text}")
```

### Navigate by Page / Section

```python
from kaos_pdf import extract_pdf
from kaos_content.views import DocumentView

doc = extract_pdf("report.pdf")
view = DocumentView(doc)

# Pages
for page in view.pages:
    print(f"Page {page.page_number}: {len(page.blocks)} blocks")

# Sections
for section in view.flat_sections:
    print(f"{'  ' * section.depth}{section.heading_text} (pages {section.page_range})")

# Single page as markdown
print(view.page_as_markdown(1))
```

### Metadata and Outline

```python
from kaos_pdf import get_pdf_metadata, get_pdf_outline, get_page_count

meta = get_pdf_metadata("report.pdf")
outline = get_pdf_outline("report.pdf")
pages = get_page_count("report.pdf")
```

## MCP Server (Claude Code)

### Setup

Add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "kaos-pdf": {
      "command": "kaos-pdf-serve"
    }
  }
}
```

Or start manually:

```bash
kaos-pdf serve              # stdio transport
kaos-pdf serve --http       # HTTP on localhost:8000
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| **ParsePDF** | Extract PDF → ContentDocument artifact with markdown, outline, sections |
| **SearchDocument** | Search within an extracted document by query |
| **GetPageText** | Get plain text from a single page |
| **RenderPage** | Render a page as an image |
| **PDFMetadata** | Get document metadata and classification |

### Workflow

1. Call `ParsePDF` with a file path → returns artifact_id + summary
2. Read resources: `kaos://content/{id}/markdown`, `kaos://content/{id}/sections`, etc.
3. Call `SearchDocument` with artifact_id + query → ranked results with page/section context

### 12 Content Resource Templates

After parsing, access document views via MCP resources:

| Resource | Returns |
|----------|---------|
| `kaos://content/{id}/markdown` | Full markdown |
| `kaos://content/{id}/metadata` | Document metadata |
| `kaos://content/{id}/outline` | Heading hierarchy |
| `kaos://content/{id}/pages` | Page index |
| `kaos://content/{id}/pages/{n}` | Single page markdown |
| `kaos://content/{id}/sections` | Section tree |
| `kaos://content/{id}/sections/{ref}` | Single section markdown |
| `kaos://content/{id}/tables` | Table summaries |
| `kaos://content/{id}/annotations` | Annotations |
| `kaos://content/{id}/node/{ref}` | Single AST node |
