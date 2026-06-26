# `english_words.txt.gz` — provenance

`kaos_pdf/data/english_words.txt.gz` is a gzip-compressed, newline-delimited
list of lowercase ASCII English words. It backs the OCR text-quality heuristic
in `kaos_pdf/quality.py`, which `extract_pdf(..., ocr="auto")` uses to decide
whether a scanned page's *native* text layer is too garbled to trust and should
be re-OCR'd.

## Source

Derived from **SCOWL** (Spell Checker Oriented Word Lists), the `american-english`
list shipped by the Debian `wamerican` package (SCOWL size 60).

- Project: <http://wordlist.sourceforge.net/>
- Maintainer: Kevin Atkinson

## License

SCOWL is freely redistributable under a permissive MIT/BSD-style notice
(reproduced below), which is compatible with this package's Apache-2.0 license.

> The collective work is Copyright 2000-2011 by Kevin Atkinson.
>
> Permission to use, copy, modify, distribute and sell these word lists, the
> associated scripts, the output created from the scripts, and its
> documentation for any purpose is hereby granted without fee, provided that
> the above copyright notice appears in all copies and that both that copyright
> notice and this permission notice appear in supporting documentation. Kevin
> Atkinson makes no representations about the suitability of this array for any
> purpose. It is provided "as is" without express or implied warranty.

## Generation procedure

```python
import gzip, pathlib
src = pathlib.Path("/usr/share/dict/american-english").read_text(errors="ignore").splitlines()
words = set()
for w in src:
    w = w.strip().lower()
    if w.endswith("'s"):
        w = w[:-2]
    if 2 <= len(w) <= 18 and w.isalpha() and w.isascii():
        words.add(w)
data = ("\n".join(sorted(words)) + "\n").encode()
pathlib.Path("kaos_pdf/data/english_words.txt.gz").write_bytes(gzip.compress(data, 9))
```

This keeps lowercase ASCII alphabetic words of length 2–18, strips trailing
`'s`, and sorts the result. The list is used only as a legibility signal
(dictionary hit-rate per text line); it is not a spell checker and intentionally
omits proper nouns, legal citations, and rare terms, which the heuristic
tolerates because it scores the *worst* substantial line per page, not every
token.
