"""Deterministic checks for AI-agent context compaction artifacts."""

from .core import (
    SCHEMA_VERSION,
    CaseError,
    Finding,
    Report,
    analyze_case,
    explain_checks,
    load_case,
    render_markdown,
    render_text,
)

__all__ = [
    "SCHEMA_VERSION",
    "CaseError",
    "Finding",
    "Report",
    "analyze_case",
    "explain_checks",
    "load_case",
    "render_markdown",
    "render_text",
]

__version__ = "0.1.0"
