"""Letters a PDF's own font table leaves out, recovered from what else the file says.

Word's Calibri draws some letter pairs as one glyph, and a composite font's ``/ToUnicode``
CMap says what each means::

    <019F> <00740069>    code 0x019F is the two letters "ti"

Acrobat-written files often split one typeface into several font subsets and give some of
them a CMap missing these entries, so nothing in that font says what the glyph is. PDFium
then reports the code itself as if it were a Unicode value ("addicƟve"), and other readers
drop the letters ("addic ve"). The text can't be read from that font, but the file usually
says it elsewhere: with ``Identity-H`` a code is the glyph's number in the typeface, so a
sibling subset of the same typeface that does map the code gives its letters. Where no
sibling does, a short table holds Calibri's own ligature glyphs, since a file written by
Word is the common case.

PDFium's API exposes no CMap, so this reads them from the file. It's a small reader on
purpose: only flate-compressed or plain streams, only the objects a CMap needs, and bounded
throughout. A file it can't read (encrypted, or an unusual filter) yields nothing, and the
text stays as PDFium reported it. A code a font's own CMap maps is never overridden.
"""

from __future__ import annotations

import re
import zlib

_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_STREAM_BYTES = 8 * 1024 * 1024
_MAX_OBJECT_STREAMS = 20_000
_MAX_EXPANSION = 8  # letters a code may stand for; a longer one isn't a ligature, and is left alone

_OBJECT = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")
_FONT_NAME = re.compile(rb"/BaseFont\s*/([^\s/<>\[\]()]+)")
_TO_UNICODE = re.compile(rb"/ToUnicode\s+(\d{1,10})\s+\d{1,5}\s+R")
_HEX = re.compile(rb"<\s*([0-9A-Fa-f\s]*)>")
_SECTION = re.compile(rb"begin(bfchar|bfrange)(.*?)end\1", re.DOTALL)
# Calibri's ligature glyphs, by glyph number, for a Word file whose subsets don't say.
# Regular, bold and italic share them.
_CALIBRI = {0x129: "fb", 0x14C: "ft", 0x19E: "tf", 0x19F: "ti", 0x1A9: "tt", 0x1AB: "tti"}


def _inflate(data: bytes) -> bytes | None:
    """A flate stream's bytes, bounded; None if it isn't one or runs past the bound."""
    try:
        out = zlib.decompressobj().decompress(data, _MAX_STREAM_BYTES)
    except zlib.error:
        return None
    return out if len(out) < _MAX_STREAM_BYTES else None


def _stream_of(body: bytes) -> bytes | None:
    """An object's stream, decoded: flate or none. Any other filter yields None."""
    start = body.find(b"stream")
    if start < 0:
        return None
    head = body[:start]
    data = body[start + len(b"stream") :]
    data = data[2:] if data[:2] == b"\r\n" else data[1:] if data[:1] in (b"\n", b"\r") else data
    end = data.rfind(b"endstream")
    if end >= 0:
        data = data[:end]
    if b"/Filter" not in head:
        return data[:_MAX_STREAM_BYTES]
    filters = re.findall(
        rb"/([A-Za-z0-9]+)",
        head[head.find(b"/Filter") + len(b"/Filter") :].split(b"/DecodeParms")[0],
    )
    if not filters or filters[0] != b"FlateDecode":
        return None
    return _inflate(data)


def _objects(data: bytes) -> dict[int, bytes]:
    """Each object's bytes by its number, those packed in object streams too. A later one
    replaces an earlier one, as an incremental update does."""
    found: dict[int, bytes] = {}
    marks = list(_OBJECT.finditer(data))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(data)
        found[int(m[1])] = data[m.end() : end]
    packed = 0
    for body in list(found.values()):
        head = body[: body.find(b"stream")] if b"stream" in body else b""
        if b"/ObjStm" not in head:
            continue
        packed += 1
        if packed > _MAX_OBJECT_STREAMS:
            break
        first = re.search(rb"/First\s+(\d+)", head)
        count = re.search(rb"/N\s+(\d+)", head)
        stream = _stream_of(body)
        if stream is None or first is None or count is None:
            continue
        offset = int(first[1])
        numbers = re.findall(rb"\d+", stream[:offset])
        pairs = list(zip(numbers[0::2], numbers[1::2], strict=False))[: int(count[1])]
        for j, (number, at) in enumerate(pairs):
            start = offset + int(at)
            stop = offset + int(pairs[j + 1][1]) if j + 1 < len(pairs) else len(stream)
            found.setdefault(int(number), stream[start:stop])
    return found


def _letters(hex_value: bytes) -> str | None:
    """A destination's UTF-16BE hex as text, or None if it isn't well formed."""
    digits = re.sub(rb"\s", b"", hex_value)
    if len(digits) % 4:
        return None
    try:
        return bytes.fromhex(digits.decode("ascii")).decode("utf-16-be")
    except (ValueError, UnicodeDecodeError):
        return None


def _is_expansion(text: str | None) -> bool:
    return text is not None and 1 < len(text) <= _MAX_EXPANSION and text.isprintable()


def _parse_cmap(cmap: bytes) -> dict[int, str]:
    """Everything a CMap maps: a code to its text."""
    out: dict[int, str] = {}
    for kind, body in _SECTION.findall(cmap):
        if kind == b"bfchar":
            hexes = _HEX.findall(body)
            for source, target in zip(hexes[0::2], hexes[1::2], strict=False):
                text = _letters(target)
                if source.strip() and text:
                    out[int(re.sub(rb"\s", b"", source), 16)] = text
            continue
        # <lo> <hi> <dest>, where dest's last unit counts up, or <lo> <hi> [<a> <b> ...]
        for line in re.finditer(
            rb"<([0-9A-Fa-f\s]+)>\s*<([0-9A-Fa-f\s]+)>\s*(<[0-9A-Fa-f\s]*>|\[[^\]]*\])", body
        ):
            lo, hi = int(re.sub(rb"\s", b"", line[1]), 16), int(re.sub(rb"\s", b"", line[2]), 16)
            if hi < lo or hi - lo > 0xFFFF:
                continue
            if line[3].startswith(b"["):
                for k, target in enumerate(_HEX.findall(line[3])):
                    text = _letters(target)
                    if text:
                        out[lo + k] = text
                continue
            start = _letters(_HEX.findall(line[3])[0])
            if not start:
                continue
            for k in range(hi - lo + 1):
                last = ord(start[-1]) + k
                if last < 0xD800:
                    out[lo + k] = start[:-1] + chr(last)
    return out


def _family(name: str) -> str:
    """A font's typeface: ``PIFAHD+Calibri-Bold`` is ``Calibri``. A style keeps its numbers."""
    return name.partition("+")[2].partition("-")[0] if "+" in name else name.partition("-")[0]


def multi_letter_maps(data: bytes) -> dict[str, dict[int, str]]:
    """For each composite font by its ``/BaseFont`` name, the codes its own CMap leaves out
    and what they stand for, from a sibling of the same typeface, else Calibri's table.
    Empty for a file it won't read."""
    if len(data) > _MAX_FILE_BYTES:
        return {}
    objects = _objects(data)
    trailers = (body for body in objects.values() if b"/XRef" in body[:512])
    if b"/Encrypt" in data[-4096:] or any(b"/Encrypt" in body[:2048] for body in trailers):
        return {}
    own: dict[str, dict[int, str]] = {}
    parsed: dict[int, dict[int, str]] = {}
    for body in objects.values():
        head = body[: body.find(b"stream")] if b"stream" in body else body
        name = _FONT_NAME.search(head)
        if name is None or b"/Type0" not in head.replace(b" ", b""):
            continue
        codes = own.setdefault(name[1].decode("latin-1"), {})
        reference = _TO_UNICODE.search(head)
        if reference is None:
            continue
        number = int(reference[1])
        if number not in parsed:
            cmap = _stream_of(objects[number]) if number in objects else None
            parsed[number] = _parse_cmap(cmap) if cmap else {}
        codes.update(parsed[number])
    # what each typeface says, across its subsets: a code two subsets disagree about isn't trusted
    said: dict[str, dict[int, str | None]] = {}
    for name, codes in own.items():
        family = said.setdefault(_family(name), {})
        for code, text in codes.items():
            if _is_expansion(text):
                family[code] = text if family.get(code, text) == text else None
    out: dict[str, dict[int, str]] = {}
    for name, codes in own.items():
        family = _family(name)
        known = {
            **(_CALIBRI if family == "Calibri" else {}),
            **{c: t for c, t in said.get(family, {}).items() if t},
        }
        missing = {code: text for code, text in known.items() if code not in codes}
        if missing:
            out[name] = missing
    return out
