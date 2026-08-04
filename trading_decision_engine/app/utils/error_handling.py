"""Centralized error handling: safe_analyze() wraps every engine call so one engine's
unexpected exception never crashes the tick pipeline, and RetryPolicy backs the broker
adapter's REST calls. See docs/DESIGN.md §9.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from ..config.constants import Direction
from ..models.engine_results import EngineResult

logger = logging.getLogger("trading_decision_engine")

T = TypeVar("T", bound=EngineResult)


class FatalBrokerError(Exception):
    """Raised for unrecoverable broker failures (auth failure, or a 401 that persists
    past one re-login attempt) that must halt the Orchestrator (-> STOPPED, see
    docs/DESIGN.md §6/§9) rather than being swallowed like an ordinary engine failure.
    """


class EngineFailureTracker:
    """Counts consecutive failures per engine name so the Orchestrator can escalate
    (pause new entries) once a single engine keeps failing, without crashing on the
    first occurrence.
    """

    def __init__(self, escalation_threshold: int = 3) -> None:
        self._escalation_threshold = escalation_threshold
        self._consecutive_failures: dict[str, int] = {}

    def record_failure(self, engine_name: str) -> bool:
        """Returns True if this engine has now crossed the escalation threshold."""
        count = self._consecutive_failures.get(engine_name, 0) + 1
        self._consecutive_failures[engine_name] = count
        return count >= self._escalation_threshold

    def record_success(self, engine_name: str) -> None:
        self._consecutive_failures[engine_name] = 0


def safe_analyze(
    engine_name: str,
    fn: Callable[..., T],
    neutral_result_factory: Callable[[str], T],
    tracker: EngineFailureTracker | None = None,
    *args,
    **kwargs,
) -> tuple[T, bool]:
    """Calls fn(*args, **kwargs); on an unexpected exception, logs it and returns a
    neutral/low-confidence result built by neutral_result_factory(reason) instead of
    propagating. Returns (result, escalate) where escalate is True once the same
    engine has failed past the tracker's threshold.
    """
    try:
        result = fn(*args, **kwargs)
        if tracker is not None:
            tracker.record_success(engine_name)
        return result, False
    except Exception as exc:  # noqa: BLE001 - deliberately broad, this is the safety net
        logger.exception("Engine %s raised an unexpected exception", engine_name)
        escalate = tracker.record_failure(engine_name) if tracker is not None else False
        reason = f"{engine_name} error: {exc}"
        return neutral_result_factory(reason), escalate


def neutral_engine_result(result_cls, reason: str, **extra_fields) -> EngineResult:
    """Builds a low-confidence, NEUTRAL-direction instance of any EngineResult subclass,
    filling any additional required fields with safe defaults supplied by the caller.
    """
    base = {
        "direction": Direction.NEUTRAL,
        "score": 0.0,
        "confidence": 0.0,
        "reasons": (reason,),
    }
    base.update(extra_fields)
    return result_cls(**base)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    rate_limit_delay_seconds: float = 4.0

    def run(self, fn: Callable[[], T], is_rate_limited: Callable[[Exception], bool] | None = None) -> T:
        """Runs fn() with bounded exponential backoff. If is_rate_limited(exc) returns
        True for a raised exception, sleeps rate_limit_delay_seconds instead of the
        exponential delay before retrying (mirrors Groww's documented 429 handling).
        """
        delay = self.base_delay_seconds
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == self.max_attempts:
                    break
                sleep_for = self.rate_limit_delay_seconds if is_rate_limited and is_rate_limited(exc) else delay
                logger.warning("Retry %d/%d after error: %s (sleeping %.1fs)", attempt, self.max_attempts, exc, sleep_for)
                time.sleep(sleep_for)
                delay *= self.backoff_multiplier
        assert last_exc is not None
        raise last_exc
