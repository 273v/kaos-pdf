"""PDFium text-marker handling: end-of-line hyphens, leaders, control characters.

PDFium does not return the hyphen that ends a hyphenated line as ``-``. When a
text line ends in a hyphen (``-``, U+00AD soft hyphen, ...) and the text
continues on the next line, ``CPDF_TextPage`` replaces that character with the
marker U+0002 (``FPDFText_IsHyphen`` reports it); older PDFium builds used the
noncharacter U+FFFE in the same position. Fonts with broken ``ToUnicode``
maps additionally surface glyphs as raw C0 control codes; dot leaders in
tables of contents commonly come back as runs of ``\\x01`` / ``\\x02``.

Nothing here is a regex over finished prose. The helpers work on PDFium's own
output at the line-rectangle level (one rect per visual line) or, for
:func:`clean_page_text`, character by character on PDFium's page text:

* :func:`join_hyphenated_lines` merges a line that ends with the marker with
  the following line when that line starts with a lowercase letter. The word
  fragments are joined without a hyphen and without a line break, the two
  rects become one, and the bbox is the union, so provenance stays truthful.
* :func:`clean_line_text` renders whatever marker is left as a visible ``-``,
  turns leader runs into ``.``, and drops every other C0 control character
  except ``\\t``, ``\\n`` and ``\\r``.

PDFium marks typeset compound hyphens at a line end the same way as
discretionary ones (``postal-service`` arrives as ``postal\\x02`` +
``service``), and its per-character flags (``FPDFText_IsHyphen``) do not
tell them apart either. :func:`hyphen_join_decision`, the default
:data:`HyphenResolver`, therefore decides with the packaged SCOWL word list
and no corpus statistics: it drops the hyphen only when the joined form is a
dictionary word, and keeps ``-`` (and, when unsure, the line break) otherwise. Callers with better
evidence (corpus counts, a domain lexicon) pass their own resolver.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from kaos_pdf.quality import _english_words

#: PDFium's end-of-line hyphen marker (current builds).
PDFIUM_HYPHEN_MARKER = "\x02"
#: The same marker as emitted by older PDFium builds.
PDFIUM_LEGACY_HYPHEN_MARKER = "\ufffe"
#: Unicode SOFT HYPHEN; at a line end it means the same thing as the markers.
SOFT_HYPHEN = "\u00ad"

#: Characters that, at the end of a line, mark a word broken across lines.
LINE_END_HYPHEN_MARKERS = frozenset(
    {PDFIUM_HYPHEN_MARKER, PDFIUM_LEGACY_HYPHEN_MARKER, SOFT_HYPHEN}
)

#: Control glyphs PDFium returns for dot leaders (runs of them in a ToC line).
_LEADER_CHARS = frozenset({"\x01", "\x02"})
#: Minimum number of leader glyphs in a run before it is treated as a leader.
_MIN_LEADER_RUN = 2
#: C0 controls whose meaning is whitespace; mapped to a space.
_WHITESPACE_CONTROLS = frozenset({"\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x1f"})
#: C0 controls that are legitimate in output text.
_ALLOWED_CONTROLS = frozenset({"\t", "\n", "\r"})

#: Function words that never start the second half of a compound. A marker
#: line followed by one of these is an interleaving artefact (table cells or
#: columns out of reading order), not a word continuation.
_NON_COMPOUND_HEADS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "between", "but", "by", "can",
        "for", "from", "has", "have", "in", "includes", "is", "it", "may", "of",
        "on", "or", "such", "than", "that", "the", "their", "these", "this", "to",
        "was", "were", "which", "with", "would",
    }
)  # fmt: skip

Rect = tuple[str, tuple[float, float, float, float], dict[str, Any] | None]
JoinDecision = Literal["join", "hyphen"]

#: Decides a word broken at a PDFium hyphen marker. Called with the fragment
#: before the marker (letters, possibly with inner hyphens, e.g. ``"day-to"``)
#: and the run of letters that starts the next line. Returns ``"join"`` (drop
#: the hyphen and the line break), ``"hyphen"`` (keep ``-``, drop the line
#: break), or ``None`` (not a word continuation: keep ``-`` and the break).
HyphenResolver = Callable[[str, str], JoinDecision | None]


def _is_dropped_control(ch: str) -> bool:
    o = ord(ch)
    return (o < 0x20 or o == 0x7F) and ch not in _ALLOWED_CONTROLS


def _trailing_word(text: str) -> str:
    """Return the run of letters and inner hyphens that ends ``text``."""
    i = len(text)
    while i > 0 and (text[i - 1].isalpha() or (text[i - 1] == "-" and i < len(text))):
        i -= 1
    return text[i:].lstrip("-")


def _leading_word(text: str) -> str:
    """Return the run of letters that starts ``text``."""
    i = 0
    while i < len(text) and text[i].isalpha():
        i += 1
    return text[:i]


def hyphen_join_decision(left: str, right: str) -> JoinDecision | None:
    """Decide how to rejoin ``left`` + marker + ``right`` across a line break.

    ``left`` is the word fragment before the marker (letters, possibly with
    inner hyphens) and ``right`` the run of letters that starts the next line.
    Returns ``"join"`` (drop the marker), ``"hyphen"`` (a typeset compound:
    keep ``-`` but rejoin the lines), or ``None`` (not a word continuation:
    render ``-`` and keep the line break).

    Lowercase continuation (``right`` starts lowercase):

    * ``left`` is a hyphenated chain (``self-regu`` / ``day-to``): its last
      component decides. Join when last + ``right`` is a dictionary word and
      the two are not both words themselves (``self-regu`` + ``latory`` ->
      ``self-regulatory``); otherwise keep the hyphen when ``right`` is a word
      (``day-to`` + ``day`` -> ``day-to-day``), else ``None``.
    * the joined form is a dictionary word (``repre`` + ``sentatives``,
      ``with`` + ``out``): join.
    * both halves are dictionary words of two or more letters but the joined
      form is not (``postal`` + ``service``, ``well`` + ``being``): a
      compound; keep ``-`` and rejoin the lines. A function word (``and``,
      ``of``, ``the``, ...) never starts a compound's second half, so it
      only ever joins into a dictionary word (``with`` + ``out``).
    * anything else is unsure (a term outside the word list, or a next line
      that is not the continuation at all, as when table cells or columns
      interleave in reading order): ``None``, so ``-`` and the line break
      stay and no unrelated lines are merged.

    Uppercase or digit continuation: ``None`` (the hyphen and the line break
    stay), except an all-caps word broken between two all-caps fragments
    whose join is a dictionary word (``DOCU`` + ``MENT``), which joins.
    """
    if not left or not right:
        return None
    words = _english_words()
    if not right[0].islower():
        if (
            len(left) >= 2
            and len(right) >= 2
            and left.isupper()
            and right.isupper()
            and "-" not in left
            and (left + right).lower() in words
        ):
            return "join"
        return None
    rw = right.lower()
    if rw in _NON_COMPOUND_HEADS:
        # "prod-" + "and ...": the next line in reading order is not the rest
        # of the word. Joining is still right when it forms a word ("be" +
        # "cause", "with" + "out" are handled below via the joined form).
        tail = left.rsplit("-", 1)[-1].lower()
        return "join" if (tail + rw) in words and "-" not in left else None
    if "-" in left:
        last = left.rsplit("-", 1)[1].lower()
        if (last + rw) in words and not (last in words and rw in words):
            return "join"
        return "hyphen" if rw in words else None
    lw = left.lower()
    if (lw + rw) in words:
        return "join"
    if len(lw) >= 2 and len(rw) >= 2 and lw in words and rw in words:
        return "hyphen"
    return None


def ends_with_line_hyphen(text: str) -> bool:
    """Return whether ``text`` ends (ignoring spaces) with a line-end hyphen marker.

    A trailing run of two or more leader glyphs is a dot leader, not a hyphen.
    """
    stripped = text.rstrip(" \t")
    if not stripped or stripped[-1] not in LINE_END_HYPHEN_MARKERS:
        return False
    return not (len(stripped) >= 2 and stripped[-2] in _LEADER_CHARS)


def join_hyphenated_lines(
    rects: Sequence[Rect], *, resolver: HyphenResolver | None = None
) -> list[Rect]:
    """Merge lines broken by a PDFium end-of-line hyphen marker.

    ``rects`` are visual lines in reading order (``(text, bbox, font_info)``
    with PDF-space ``(left, bottom, right, top)`` boxes). When a line ends with
    a marker from :data:`LINE_END_HYPHEN_MARKERS` and the next line continues
    the word (it starts with a lowercase letter; see
    :func:`hyphen_join_decision`), the two become one rect: the marker is
    removed (or replaced by ``-`` for a real compound hyphen), no space or line
    break is inserted, the bbox is the union of both boxes, and ``font_info``
    comes from the first line.

    Lines whose marker is followed by an uppercase letter, a digit, or nothing
    (bar the all-caps case in :func:`hyphen_join_decision`) are left untouched
    here; :func:`clean_line_text` renders that marker as a
    visible ``-`` and the line break stays.

    ``resolver`` replaces :func:`hyphen_join_decision` for the decision.
    """
    decide = resolver if resolver is not None else hyphen_join_decision
    out: list[Rect] = []
    for text, bbox, fi in rects:
        if out:
            prev_text, prev_bbox, prev_fi = out[-1]
            nxt = text.lstrip(" \t")
            head = prev_text.rstrip(" \t")[:-1]
            decision = (
                decide(_trailing_word(head), _leading_word(nxt))
                if ends_with_line_hyphen(prev_text)
                else None
            )
            if decision is not None:
                sep = "-" if decision == "hyphen" else ""
                merged_bbox = (
                    min(prev_bbox[0], bbox[0]),
                    min(prev_bbox[1], bbox[1]),
                    max(prev_bbox[2], bbox[2]),
                    max(prev_bbox[3], bbox[3]),
                )
                out[-1] = (head + sep + nxt, merged_bbox, prev_fi)
                continue
        out.append((text, bbox, fi))
    return out


def clean_line_text(text: str) -> tuple[str, int]:
    """Render PDFium markers and strip control characters from one line of text.

    * runs of two or more leader glyphs (``\\x01`` / ``\\x02``, optionally
      separated by spaces) become ``.`` (one per glyph), the visible leader;
    * any remaining hyphen marker (U+0002, U+FFFE, or U+00AD at a word end)
      becomes ``-``; a U+00AD inside a word was an unused discretionary break
      and is removed;
    * ``\\x0b``, ``\\x0c`` and the C0 separators become a space;
    * every other C0 control (and DEL) except ``\\t``, ``\\n``, ``\\r`` is
      dropped.

    Returns ``(text, dropped)`` where ``dropped`` counts the control
    characters removed without a mapping, so callers can log them.
    """
    if not any(_is_dropped_control(c) or c in LINE_END_HYPHEN_MARKERS for c in text):
        return text, 0
    out: list[str] = []
    dropped = 0
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch in _LEADER_CHARS:
            # Measure a leader run: leader glyphs, optionally space-separated.
            j = i
            glyphs = 0
            while j < n and (text[j] in _LEADER_CHARS or text[j] == " "):
                if text[j] != " ":
                    glyphs += 1
                j += 1
            while j > i and text[j - 1] == " ":
                j -= 1
            if glyphs >= _MIN_LEADER_RUN:
                out.extend("." if text[k] != " " else " " for k in range(i, j))
                i = j
                continue
        if ch in (PDFIUM_HYPHEN_MARKER, PDFIUM_LEGACY_HYPHEN_MARKER):
            out.append("-")
        elif ch == SOFT_HYPHEN:
            if i + 1 < n and text[i + 1].isalpha():
                pass  # unused discretionary break inside a word
            else:
                out.append("-")
        elif ch in _WHITESPACE_CONTROLS:
            out.append(" ")
        elif _is_dropped_control(ch):
            dropped += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out), dropped


def clean_page_text(
    text: str,
    *,
    join_hyphenated_lines: bool = True,
    resolver: HyphenResolver | None = None,
) -> tuple[str, int]:
    """Clean PDFium whole-page text (``FPDFText_GetBoundedText`` over the page).

    For page text PDFium already removes the line break after a hyphenated
    line, so the marker sits between the two fragments (``di\\x02rectly``).
    Where the marker sits between two letters it is resolved with
    :func:`hyphen_join_decision` (removed on ``"join"``, ``-`` otherwise); with
    ``join_hyphenated_lines=False`` it always becomes ``-``. The rest of the
    text goes through :func:`clean_line_text`.
    """
    decide = resolver if resolver is not None else hyphen_join_decision
    if join_hyphenated_lines and any(c in LINE_END_HYPHEN_MARKERS for c in text):
        parts: list[str] = []
        n = len(text)
        for i, ch in enumerate(text):
            if (
                ch in (PDFIUM_HYPHEN_MARKER, PDFIUM_LEGACY_HYPHEN_MARKER)
                and 0 < i < n - 1
                and text[i - 1].isalpha()
                and text[i + 1].isalpha()
            ):
                left = _trailing_word(text[max(0, i - 64) : i])
                right = _leading_word(text[i + 1 : i + 65])
                decision = decide(left, right)
                if decision != "join":
                    parts.append("-")
                continue
            parts.append(ch)
        text = "".join(parts)
    return clean_line_text(text)
