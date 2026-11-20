"""Trace logs: every model call of a session, saved as JSON Lines, plus a readable timeline.

Each line is one `TraceEvent`: time, step, attempt, success, tokens, latency, cost, stop reason and
error. Logs hold call metadata only, never prompts, story text or child details, and they're written
outside the repo (`config.LOG_DIR`).
"""

from __future__ import annotations

from pathlib import Path

from . import config
from .models import StorySession, TraceEvent


def log_path(session: StorySession, log_dir: Path | None = None) -> Path:
    return (log_dir or config.LOG_DIR) / f"{session.created_at:%Y%m%d-%H%M%S}-{session.id}.jsonl"


def save(session: StorySession, log_dir: Path | None = None) -> Path | None:
    """Write the session's full trace (rewritten after each run, so it's always complete).

    Returns the file path, or None if it couldn't be written: logging never breaks a story.
    """
    path = log_path(session, log_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(event.model_dump_json() + "\n" for event in session.trace))
    except OSError:
        return None
    return path


def load(path: Path) -> list[TraceEvent]:
    return [TraceEvent.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]


def timeline(trace: list[TraceEvent]) -> str:
    """A plain-text table of the calls, with totals."""
    header = f"{'#':>3}  {'step':<9} {'try':>3}  {'ok':<4} {'in':>6} {'out':>6} {'secs':>6} {'cost':>9}  stop"
    lines = [header, "-" * len(header)]
    for i, e in enumerate(trace, 1):
        status = "yes" if e.ok else "NO"
        stop = e.error or e.stop_reason or ""
        lines.append(
            f"{i:>3}  {e.step:<9} {e.attempt:>3}  {status:<4} {e.input_tokens:>6} {e.output_tokens:>6} "
            f"{e.latency_ms / 1000:>6.1f} ${e.cost_usd:>8.5f}  {stop}"
        )
    total_in = sum(e.input_tokens for e in trace)
    total_out = sum(e.output_tokens for e in trace)
    total_secs = sum(e.latency_ms for e in trace) / 1000
    total_cost = sum(e.cost_usd for e in trace)
    lines += [
        "-" * len(header),
        f"{'':>3}  {'total':<9} {len(trace):>3}  {'':<4} {total_in:>6} {total_out:>6} {total_secs:>6.1f} ${total_cost:>8.5f}",
    ]
    return "\n".join(lines)
