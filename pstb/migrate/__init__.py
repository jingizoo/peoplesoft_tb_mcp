"""9.1 -> 9.2 record-porting pipeline: discover custom records on the old
instance, close over their dependencies, classify against the new instance,
emit App Designer / Data Mover artifacts, and verify the result read-only.

Entry points: `python -m pstb.migrate` (CLI) and `python -m pstb.migrate.server`
(MCP, for the Gemini/Ollama chat client or any MCP host). Design and runbook:
docs/MIGRATION_PIPELINE.md.
"""
from .pipeline import MigratePipeline
from .spec import MigrateError

__all__ = ["MigratePipeline", "MigrateError"]
