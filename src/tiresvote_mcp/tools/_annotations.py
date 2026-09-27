"""Shared ToolAnnotations for tool modules — every TiresVote tool is read-only."""

from mcp.types import ToolAnnotations

CATALOG_ANNOTATIONS = ToolAnnotations(
    title="Catalog (freely callable)",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

SEARCH_ANNOTATIONS = ToolAnnotations(
    title="Search",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

EVIDENCE_ANNOTATIONS = ToolAnnotations(
    title="Evidence (reviews and professional tests)",
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
