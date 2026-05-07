# kaos-pdf: Thread Safety and Parallelism

## Summary

kaos-pdf uses pypdfium2 (Python bindings to Google's PDFium C++ library). **PDFium is NOT thread-safe** — not even when operating on different documents from different threads. All PDFium calls in kaos-pdf are serialized through a global `threading.Lock` to prevent corruption under free-threaded Python (3.13t/3.14t).

The rest of the kaos stack (Pillow, numpy, pydantic, kaos-content) IS thread-safe and supports free-threading.

---

## Why PDFium Is Not Thread-Safe

PDFium's official header (`public/fpdfview.h`) states:

> "None of the PDFium APIs are thread-safe. They expect to be called from a single thread. Barring that, embedders are required to ensure (via a mutex or similar) that only a single PDFium call can be made at a time."

This is not a binding-layer problem — it is fundamental to the C++ implementation. PDFium has pervasive global mutable state:

| Global Variable | Location | Why It Breaks Threads |
|-----------------|----------|----------------------|
| `g_last_error` | `fx_system.cpp` | Single global error code — one thread's error overwrites another's |
| `CPDF_FontGlobals::stock_map_` | `cpdf_fontglobals.cpp` | Mutable map keyed by `CPDF_Document*`, mutated during normal operations on ANY document |
| `CFX_GEModule` (singleton) | `cfx_gemodule.cpp` | Entire graphics engine: font manager, glyph cache, renderer state |
| `g_bLibraryInitialized` | `fpdf_view.cpp` | Global initialization flag |
| FreeType `FT_Library` | embedded FreeType | One global instance with no internal locking |

**Key insight:** `CPDF_FontGlobals::stock_map_` is indexed by document pointer and mutated during normal document operations. Even operations on completely separate documents race on this global map. Per-document locking is not possible — only a single global mutex works.

### Chromium's Approach

Chromium (which develops PDFium) runs all PDFium calls on a **single sequenced task runner** within each renderer process. Even Google does not attempt to use PDFium from multiple threads. When multiple tabs need PDF rendering, they use separate **processes** (each with its own copy of PDFium's global state).

### Could a Rust Wrapper Fix This?

No. A Rust wrapper around PDFium (e.g., `pdfium-render` on crates.io) can add a `Mutex`, but the only safe granularity is a single global lock around ALL PDFium calls — which gives zero concurrency benefit. The `pdfium-render` crate's `thread_safe` feature marks types as `Send + Sync` but does not actually serialize access (it relies on `OnceCell` for initialization, not call-level locking).

---

## What kaos-pdf Does

All public functions in `kaos_pdf.extract` acquire `_PDFIUM_LOCK` (a `threading.Lock`) before making any pypdfium2 call:

```python
_PDFIUM_LOCK = threading.Lock()

def extract_pdf(source, ...):
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(str(path))
        try:
            return _extract_document(doc, ...)
        finally:
            doc.close()
```

This ensures correctness under:
- Standard CPython (GIL provides implicit serialization, but the lock is explicit for clarity)
- Free-threaded Python 3.13t/3.14t (no GIL — the lock is essential)
- Any async framework that might schedule PDF operations concurrently

---

## Free-Threading Support Across the Stack

| Library | Version | Thread-Safe | cp313t/cp314t Wheels | Notes |
|---------|---------|-------------|---------------------|-------|
| **pypdfium2** | 5.6.0 | **NO** (global C++ state) | N/A (pure Python ctypes) | Serialized via `_PDFIUM_LOCK` |
| **Pillow** | 12.1.1 | YES | YES | Arena locking since 11.0; safe for separate images per thread |
| **numpy** | 2.4.3 | YES (separate arrays) | YES | Safe when each thread has its own arrays |
| **pydantic** | 2.11+ | YES (experimental) | YES (via pydantic-core) | Safe for validate/serialize; model classes defined at import |
| **kaos-content** | 0.1.0 | YES | Pure Python | Frozen Pydantic models; immutable AST |
| **kaos-nlp-core** | 0.1.0 | YES | Custom (PyO3 + Rust) | Uses `py.allow_threads` for GIL release |

---

## Parallelism Recommendations

### Pattern 1: Sequential Extract, Parallel Post-Process (Recommended)

PDFium extraction is fast (17ms for 1 page, 273ms for 34 pages). The bottleneck in agentic workflows is downstream processing (LLM calls, OCR, similarity search). Use threads for the parallel-safe parts:

```python
from concurrent.futures import ThreadPoolExecutor
from kaos_pdf import extract_pdf, render_page
from kaos_content.images.profiles import for_ocr, for_vlm

# Sequential: extract with PDFium (fast, serialized)
doc = extract_pdf("contract.pdf")
pages = list(range(get_page_count("contract.pdf")))

# Sequential: render pages (PDFium, serialized)
images = [render_page("contract.pdf", p, dpi=300) for p in pages]

# Parallel: image preprocessing (Pillow — thread-safe)
with ThreadPoolExecutor() as pool:
    ocr_images = list(pool.map(for_ocr, images))
    vlm_images = list(pool.map(for_vlm, images))
```

### Pattern 2: Process Pool for PDF-Level Parallelism

When processing many independent PDF files, use `ProcessPoolExecutor`. Each process gets its own copy of PDFium's global state:

```python
from concurrent.futures import ProcessPoolExecutor
from kaos_pdf import extract_pdf

pdf_paths = ["doc1.pdf", "doc2.pdf", "doc3.pdf", ...]

with ProcessPoolExecutor(max_workers=4) as pool:
    documents = list(pool.map(extract_pdf, pdf_paths))
```

**Trade-off:** IPC serialization overhead for `ContentDocument` objects (they're Pydantic models, so JSON serialization is required to cross process boundaries). For large documents this can be significant.

### Pattern 3: Async with Single Worker

In async frameworks (FastAPI, kaos-mcp), run PDFium operations in a thread pool with `max_workers=1`:

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

_pdf_executor = ThreadPoolExecutor(max_workers=1)

async def extract_pdf_async(path):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_pdf_executor, extract_pdf, path)
```

### What NOT to Do

- **Do not** call `extract_pdf`, `render_page`, or any other kaos-pdf function from multiple threads without the lock. Even though the lock is built into the public API, avoid creating your own `pdfium.PdfDocument` instances outside the lock.
- **Do not** try to use per-document locking — PDFium's globals are shared across all documents.
- **Do not** assume a Rust wrapper would help — the C++ code itself is the problem, not the binding layer.

---

## Performance Context

PDF text extraction is rarely the bottleneck in agentic workflows:

| Operation | Time | Context |
|-----------|------|---------|
| Extract 1-page PDF | 17 ms | Faster than a single LLM token |
| Extract 34-page PDF | 273 ms | Faster than one LLM API call |
| Render page at 300 DPI | 39 ms | Image preprocessing can be parallelized after |
| LLM API call | 500-5000 ms | The actual bottleneck |
| OCR (Tesseract) | 1-10 sec/page | Parallelizable with processes |

Sequential PDF extraction followed by parallel downstream processing is the right architecture for most use cases.

---

## References

- [PDFium `fpdfview.h` — official thread safety statement](https://pdfium.googlesource.com/pdfium/+/refs/heads/main/public/fpdfview.h)
- [Chromium issue 40147080 — "Support multiple PDF plugin threads"](https://issues.chromium.org/issues/40147080) (still open)
- [pypdfium2 issue #303 — thread safety discussion](https://github.com/pypdfium2-team/pypdfium2/issues/303)
- [PDFium Google Groups — thread safety Q&A](https://groups.google.com/g/pdfium/c/HeZSsM_KEUk)
- [pdfium-render issue #20 — Rust thread safety implementation](https://github.com/ajrcarey/pdfium-render/issues/20)
- [Python free-threading compatibility tracker](https://py-free-threading.github.io/tracking/)
