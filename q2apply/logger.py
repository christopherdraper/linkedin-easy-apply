"""Structured per-application decision logging for the Q2 agent."""

import logging
import time
from typing import Dict, List

log = logging.getLogger(__name__)


class DecisionLogger:
    """Accumulates a structured decision log for one application."""

    def __init__(self):
        self._entries: List[Dict] = []
        self._step = 0

    def log(
        self,
        action: str,
        target: str,
        value: str = "",
        reasoning: str = "",
        confidence: str = "high",
    ) -> None:
        self._step += 1
        entry = {
            "step": self._step,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "target": target,
            "value": value,
            "reasoning": reasoning,
            "confidence": confidence,
        }
        self._entries.append(entry)
        level = {"high": "INFO", "medium": "INFO", "uncertain": "WARNING"}.get(confidence, "INFO")
        getattr(log, level.lower())(
            "  [%d] %s %s -> %s (%s)", self._step, action, target, value or "-", confidence
        )

    def entries(self) -> List[Dict]:
        return list(self._entries)
