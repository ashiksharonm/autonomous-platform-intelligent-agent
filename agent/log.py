"""One JSON object per log line on stderr, always carrying the request id when known."""

import json
import logging
import sys


class JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname.lower(), "msg": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


def get_logger(name="agent"):
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def log_event(msg, **fields):
    get_logger().info(msg, extra={"fields": fields})
