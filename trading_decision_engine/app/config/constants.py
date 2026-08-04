"""Shared enums used across models and engines. See docs/DESIGN.md §3-4."""

from datetime import datetime, time
from enum import Enum

MARKET_OPEN_TIME = time(9, 15)
MARKET_CLOSE_TIME = time(15, 30)


def is_market_open_time(ts: datetime) -> bool:
    """NSE/BSE regular session check — does not account for weekends/holidays, callers
    that need that should also check ts.weekday() (5=Sat, 6=Sun) themselves.
    """
    return MARKET_OPEN_TIME <= ts.time() < MARKET_CLOSE_TIME


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class TradeAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"
    HOLD = "HOLD"
    REJECT = "REJECT"


class MarketStructure(str, Enum):
    HH_HL = "HH_HL"
    LH_LL = "LH_LL"
    DOUBLE_TOP = "DOUBLE_TOP"
    DOUBLE_BOTTOM = "DOUBLE_BOTTOM"
    SIDEWAYS = "SIDEWAYS"
    COMPRESSION = "COMPRESSION"
    EXPANSION = "EXPANSION"
    EXHAUSTION = "EXHAUSTION"


class TradeLifecycleState(str, Enum):
    IDLE = "IDLE"
    OPEN = "OPEN"
    MONITORING = "MONITORING"
    EXIT_TRIGGERED = "EXIT_TRIGGERED"
    CLOSED = "CLOSED"


class OrchestratorState(str, Enum):
    MARKET_CLOSED = "MARKET_CLOSED"
    WAIT_MODE = "WAIT_MODE"
    ANALYZING = "ANALYZING"
    CONFIRMING = "CONFIRMING"
    SIZING = "SIZING"
    ORDER_PLACING = "ORDER_PLACING"
    IN_TRADE = "IN_TRADE"
    EXITING = "EXITING"
    COOLDOWN = "COOLDOWN"
    MARKET_CLOSING = "MARKET_CLOSING"
    STOPPED = "STOPPED"


class Index(str, Enum):
    NIFTY = "NIFTY"
    BANKNIFTY = "BANKNIFTY"
    FINNIFTY = "FINNIFTY"
    SENSEX = "SENSEX"


# Strike step per underlying index (NSE/BSE convention, replicated from PROD10FEB/QA_PASS).
STRIKE_STEP = {
    Index.NIFTY: 50,
    Index.FINNIFTY: 50,
    Index.BANKNIFTY: 100,
    Index.SENSEX: 100,
}

# Exchange each index trades on.
INDEX_EXCHANGE = {
    Index.NIFTY: "NSE",
    Index.FINNIFTY: "NSE",
    Index.BANKNIFTY: "NSE",
    Index.SENSEX: "BSE",
}

DEFAULT_PRODUCT = "MIS"
