"""Constants for Assist Learner."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "assist_learner"

MIN_HA_VERSION: Final = "2026.9.0"

CONF_THRESHOLD: Final = "threshold"
CONF_EXTRA_DENYLIST: Final = "extra_denylist"
CONF_LANGUAGE: Final = "language"
CONF_REPLAY: Final = "replay_enabled"
CONF_FALLBACK_AGENT: Final = "fallback_agent_id"

DEFAULT_THRESHOLD: Final = 2

# Seconds of chat-log silence after which a turn is considered complete.
IDLE_TIMEOUT: Final = 15.0

WATCHDOG_WINDOW: Final = timedelta(days=7)
WATCHDOG_INTERVAL: Final = timedelta(hours=6)

SENTENCES_FILENAME: Final = "assist_learner.yaml"
METADATA_KEY: Final = "assist_learner_id"

ISSUE_REVIEW: Final = "review_candidates"
ISSUE_CAPTURE_PAUSED: Final = "capture_paused"
ISSUE_WATCHDOG: Final = "capture_watchdog"
ISSUE_EXPORT_FAILED: Final = "export_failed"

SERVICE_APPROVE: Final = "approve"
SERVICE_REJECT: Final = "reject"
SERVICE_FORGET: Final = "forget"
SERVICE_LEARN_LAST: Final = "learn_last"

ATTR_CANDIDATE_ID: Final = "candidate_id"
ATTR_SENTENCE: Final = "sentence"

STATUS_CANDIDATE: Final = "candidate"
STATUS_PROPOSED: Final = "proposed"
STATUS_DEFERRED: Final = "deferred"
STATUS_APPROVED: Final = "approved"
STATUS_REJECTED: Final = "rejected"

SIGNAL_UPDATED: Final = f"{DOMAIN}_updated"
