"""seshat -- a persistent note store an assistant can read and write cheaply.

Named for the Egyptian goddess of writing, measurement, and record-keeping.
See docs/seshat-mcp-spec.md for the design and its rationale.
"""

__version__ = "0.2.0"

SPEC_VERSION = "1.1"
"""The spec revision this build implements.

Reported by `seshat info`. If docs/seshat-mcp-spec.md carries a higher version
than this, the code has not caught up -- say so rather than assuming the prose
is describing what runs.
"""
