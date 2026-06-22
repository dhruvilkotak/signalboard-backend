"""
utils/logging_setup.py

Structured JSON logging for GCP Cloud Logging.

Every log line becomes a JSON object:
  {
    "severity":  "INFO" | "WARNING" | "ERROR",
    "message":   "...",
    "logger":    "services.signal_service",
    "timestamp": "2025-06-13T10:42:01.123Z",

    # enriched fields (present when set via LogContext or log_event())
    "event":     "signal_written" | "api_error" | "job_slow" | ...,
    "symbol":    "TSLA",
    "signal":    "BUY",
    "confidence":"HIGH",
    "status":    500,
    "endpoint":  "/api/signals/feed",
    "uid":       "abc123",
    "duration_ms": 312,
    "job":       "market_hours_signal_job",
    ...
  }

GCP Cloud Logging automatically maps:
  "severity" → log severity level
  "message"  → log entry text
  "timestamp"→ log entry timestamp

Log-based metric filters (set up in GCP Console):
  signal_generated   : jsonPayload.event = "signal_written"
  api_error_5xx      : jsonPayload.event = "api_error" AND jsonPayload.status >= 500
  signal_stale       : jsonPayload.event = "signal_stale"
  scheduled_job_slow : jsonPayload.event = "job_slow"
  yahoo_timeout      : jsonPayload.event = "yahoo_timeout"
  api_error_4xx      : jsonPayload.event = "api_error" AND jsonPayload.status >= 400
                       AND jsonPayload.status < 500
"""

import json
import logging
import traceback
from datetime import datetime, timezone


class GCPJsonFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects understood by GCP Cloud Logging.
    Severity levels map: DEBUG→DEBUG, INFO→INFO, WARNING→WARNING,
                         ERROR→ERROR, CRITICAL→CRITICAL.
    """

    LEVEL_MAP = {
        logging.DEBUG:    "DEBUG",
        logging.INFO:     "INFO",
        logging.WARNING:  "WARNING",
        logging.ERROR:    "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "severity":  self.LEVEL_MAP.get(record.levelno, "INFO"),
            "message":   record.getMessage(),
            "logger":    record.name,
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
        }

        # Attach any extra structured fields passed via log_event() or logger.info(..., extra={...})
        for key, val in record.__dict__.items():
            if key.startswith("_sb_"):          # our namespaced extras
                payload[key[4:]] = val          # strip "_sb_" prefix

        # Attach exception info if present
        if record.exc_info:
            payload["error"] = "".join(traceback.format_exception(*record.exc_info)).strip()

        return json.dumps(payload, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """
    Call once at app startup (in main.py lifespan) to configure structured logging.
    Replaces the basicConfig() call.
    """
    formatter = GCPJsonFormatter()

    root = logging.getLogger()
    root.setLevel(level)

    # Remove any existing handlers (e.g. from basicConfig)
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, level: str, message: str, **fields) -> None:
    """
    Emit a structured log event with named fields for GCP metric filtering.

    Usage:
        log_event(logger, "info", "Signal written to Firestore",
                  event="signal_written", symbol="TSLA", signal="BUY",
                  confidence="HIGH", session="market")

        log_event(logger, "error", "Yahoo Finance timeout",
                  event="yahoo_timeout", symbol="JEPQ", attempt=3)

        log_event(logger, "warning", "Scheduled job slow",
                  event="job_slow", job="market_hours_signal_job",
                  duration_ms=4823, threshold_ms=2000)

        log_event(logger, "error", "API error",
                  event="api_error", status=500,
                  endpoint="/api/signals/feed", uid="abc123")
    """
    extra = {f"_sb_{k}": v for k, v in fields.items()}
    log_fn = getattr(logger, level.lower(), logger.info)
    log_fn(message, extra=extra)