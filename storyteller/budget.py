"""Per-story budget: caps the calls, cost and time one run may use.

Each LLM call is already bounded (token limit, timeout, retries). This bounds the whole run, so a
story can't keep revising, or keep retrying, without limit. The pipeline checks the budget before
starting a revision; a revision is always followed by its judge round, so a started round finishes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import config
from .models import TraceEvent


@dataclass(frozen=True)
class Budget:
    max_calls: int = config.BUDGET_MAX_CALLS
    max_cost_usd: float = config.BUDGET_MAX_COST_USD
    max_seconds: float = config.BUDGET_MAX_SECONDS


class BudgetTracker:
    """Measures one run against a Budget, from the run's trace events and a clock."""

    def __init__(
        self,
        budget: Budget,
        trace: list[TraceEvent],
        clock: Callable[[], float] = time.monotonic,
        started: Optional[float] = None,
        first_event: Optional[int] = None,
    ):
        """`started` and `first_event` let a run that began before the tracker existed count from
        its real start; by default the run starts now, after any events already in the trace."""
        self.budget = budget
        self.trace = trace
        self.first_event = len(trace) if first_event is None else first_event
        self.clock = clock
        self.started = clock() if started is None else started

    @property
    def events(self) -> list[TraceEvent]:
        return self.trace[self.first_event :]

    def exceeded(self) -> Optional[str]:
        """Why the run must stop, or None if there's room for more work."""
        calls = len(self.events)
        cost = sum(e.cost_usd for e in self.events)
        seconds = self.clock() - self.started
        if calls >= self.budget.max_calls:
            return f"call limit reached ({calls} of {self.budget.max_calls} calls)"
        if cost >= self.budget.max_cost_usd:
            return f"cost limit reached (${cost:.4f} of ${self.budget.max_cost_usd:.2f})"
        if seconds >= self.budget.max_seconds:
            return f"time limit reached ({seconds:.0f} of {self.budget.max_seconds:.0f} s)"
        return None
