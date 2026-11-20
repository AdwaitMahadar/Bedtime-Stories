"""All tunable settings in one place: model, limits, quality thresholds, prices and paths."""

import os
from pathlib import Path

# --- Model --------------------------------------------------------------------
MODEL = "claude-haiku-5-5"

# Haiku 5.5 list prices (USD per million tokens, prompts up to 100K tokens).
PRICE_INPUT_PER_MTOK = 0.10
PRICE_OUTPUT_PER_MTOK = 0.50

# --- API client ---------------------------------------------------------------
REQUEST_TIMEOUT_S = 120.0
SDK_MAX_RETRIES = 2  # the SDK retries 429, 5xx and connection errors with backoff
KEY_CHECK_TIMEOUT_S = 15.0

# Per-step reasoning effort. Thinking is adaptive on Haiku 5.5; effort sets how much it thinks.
EFFORT = {
    "analyzer": "low",
    "writer": "medium",
    "judge": "medium",
    "reviser": "medium",
    "followup": "low",
}

# max_tokens covers thinking plus the answer. Doubled once if a response is cut off.
MAX_TOKENS = {
    "analyzer": 4000,
    "writer": 10000,
    "judge": 8000,
    "reviser": 10000,
    "followup": 3000,
}

# --- Quality gate -------------------------------------------------------------
MAX_ROUNDS = 3  # judge rounds per version; a 3rd runs only when a must-fix problem is still left (eval v9)
FAIL_MAX_SCORE = 2  # a rubric score at or below this fails the draft, like a must-fix issue
LENGTH_TOLERANCE = 0.25  # word count must be within ±25 % of the target; the writer still aims for the target
READING_GRADE_SLACK = 1.0  # simplify only when the reading grade is more than this above the age band

# --- Per-story budget ------------------------------------------------------------
# Limits for one run: a new story, or one follow-up. A normal run uses 3-7 calls, about $0.006
# and 45 s. Once a limit is reached, no new revision starts and the best judged draft so far is
# returned, with a note saying why.
BUDGET_MAX_CALLS = 12  # model calls, counting retried attempts
BUDGET_MAX_COST_USD = 0.03
BUDGET_MAX_SECONDS = 120.0

# --- Story shape ----------------------------------------------------------------
LENGTH_TARGETS = {"short": 350, "medium": 600, "long": 950}  # words
READ_ALOUD_WPM = 120  # a calm bedtime reading pace

# --- Local data -------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("BEDTIME_STORIES_HOME", Path.home() / ".bedtime-stories"))
LOG_DIR = DATA_DIR / "logs"  # trace logs: call metadata only, no prompts or story text
