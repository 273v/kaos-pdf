"""Internal adapter routing PDF tool inputs through kaos-core's path resolver.

Centralises the call to :func:`kaos_core.path_resolver.resolve_input_path`
so every kaos-pdf MCP tool that takes a ``path`` parameter resolves
``kaos://artifacts/<id>`` URIs, ``kaos://<vfs-scheme>/...`` URIs, and
session-VFS relative paths the same way — instead of doing raw
``Path(p).exists()`` against the process CWD and silently failing on
files uploaded through a host UI (the VFS-blind-tools failure mode
documented in
``kaos-modules/docs/plans/vfs-blind-tools-audit-and-fix-plan.md``).

Usage in a tool::

    from kaos_pdf._path_resolver import resolve_pdf_input
    from kaos_core.path_resolver import InputPathResolutionError, ResolvedOrigin

    try:
        async with resolve_pdf_input(inputs.get("path", ""), context) as resolved:
            doc = parse_pdf(resolved.path)  # ``resolved.path`` is always a real disk path
    except InputPathResolutionError as exc:
        return ToolResult.create_error(exc.to_agent_message())

When the tool runs outside a KaosContext (CLI smoke tests, ad-hoc
scripts) a stub ``KaosContext(session_id="cli")`` is synthesised so
absolute filesystem paths continue to work — a no-context path
remains a passthrough.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from kaos_core.path_resolver import resolve_input_path

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from kaos_core.base.context import KaosContext
    from kaos_core.path_resolver import ResolvedInput

# Mime types this package's tools will accept. Filesystem inputs that
# don't have a ``.pdf`` suffix slip through (the resolver only enforces
# when it can identify a mime type), so this is a soft hint rather than
# an absolute guard — corrupt-or-misnamed input still fails at parse
# time via the existing _HINT_CORRUPTED_PDF branch.
_PDF_MIMES: tuple[str, ...] = ("application/pdf",)


@asynccontextmanager
async def resolve_pdf_input(
    path_or_uri: str,
    context: KaosContext | None,
) -> AsyncIterator[ResolvedInput]:
    """Resolve a PDF file/URI input to a real ``pathlib.Path``.

    Wraps :func:`kaos_core.path_resolver.resolve_input_path` with the
    kaos-pdf-specific mime allow-list and a fallback ``KaosContext`` so
    tools invoked without a runtime (CLI smoke tests) still resolve
    absolute paths.

    Parameters
    ----------
    path_or_uri
        The path / URI / artifact reference the agent supplied.
    context
        The current ``KaosContext`` (may be ``None`` for non-MCP callers
        — a stub session is synthesised so absolute filesystem paths
        remain a passthrough).

    Yields
    ------
    ResolvedInput
        See :class:`kaos_core.path_resolver.ResolvedInput`.

    Raises
    ------
    kaos_core.path_resolver.InputPathResolutionError
        When the input cannot be resolved (empty, malformed URI,
        missing in VFS and on disk, mime-type mismatch, etc.). Callers
        should surface ``exc.to_agent_message()`` via
        ``ToolResult.create_error(...)``.
    """
    if context is None:
        from kaos_core.base.context import KaosContext as _KaosContext

        context = _KaosContext(session_id="cli")
    async with resolve_input_path(
        path_or_uri,
        context=context,
        allowed_mime_types=_PDF_MIMES,
    ) as resolved:
        yield resolved


__all__ = ["resolve_pdf_input"]
