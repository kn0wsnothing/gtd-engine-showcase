"""Local-only settings for the public GTD application."""
import os
from pathlib import Path

HOME = Path(os.environ.get("GTD_HOME", "~/.local/share/gtd-engine")).expanduser()
VAULT = HOME / "files"
DB_PATH = Path(os.environ.get("GTD_DB", str(HOME / "gtd.db"))).expanduser()
HISTORY_PATH = HOME / "history.jsonl"
COMMITMENT_TYPES = ("obligation", "intention")
WORK_TYPES = ("open", "closed")
EXECUTION_TYPES = ("interactive", "automated")
BLOCK_KINDS = ("done", "date_reached", "exists", "confirmed", "proximity", "judgment")
LANES = ("vault", "code", "rules")
DEPENDENT_ACTION_DISPOSITIONS = ("release", "drop")
DEFAULT_AGENT = "demo-agent"
STALE_SURFACING_CYCLES = 3
WAITING_DECAY_DAYS = 7
PROXIMITY_PREP_WINDOW_H = 36
BLOCK_UNEXAMINED_DAYS = 30
BLOCK_JUDGMENT_STALE_DAYS = 5
INTENTION_STATUS_CHECK_DAYS = 90
SELF_CLARIFY_WINDOW_MIN = 60
TEXT_VERB_ADVANCE_WINDOW_MIN = 60
DRIFT_EVIDENCE_LIMIT = 5
DISPATCH_STUCK_HOURS = 4
WORK_AREA_NAME = "Work"
