"""Engine(Protocol): the shared shape every analysis engine implements. Engines never
call each other directly and never touch the broker — see docs/DESIGN.md §1/§3.
"""

from __future__ import annotations

from typing import Protocol

from ..models.engine_results import EngineResult
from ..models.market_snapshot import MarketSnapshot


class Engine(Protocol):
    def analyze(self, snapshot: MarketSnapshot) -> EngineResult: ...
