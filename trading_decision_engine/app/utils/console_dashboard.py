"""Live console dashboard: renders the decision-transparency object from
utils/decision_diagnostics.py as an in-place-updating terminal panel with confidence
bars, per-engine scores/contributions, Stage-1 gate results, and full rejection detail.

Pure presentation — consumes the diagnostics dict, never touches engines or decisions.
Wired via the Orchestrator's on_diagnostics observer hook in run.py (--dashboard).
Redraws are throttled to dashboard_refresh_seconds; a BUY/SELL/EXIT event forces an
immediate redraw so trades are never missed between refreshes. Falls back to doing
nothing when stdout is not a TTY (logs already carry the same data).
"""

from __future__ import annotations

import sys
import time

BAR_WIDTH = 24
_CLEAR = "\x1b[2J\x1b[H"  # clear screen + cursor home


def bar(pct: float, width: int = BAR_WIDTH) -> str:
    """A 0-100% value as a block bar: ██████░░░░ 42%"""
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100.0 * width)
    return "█" * filled + "░" * (width - filled)


def _fmt_engine_line(name: str, info: dict) -> str:
    mark = "PASS" if info.get("passed") else "FAIL"
    contribution = info.get("contribution", 0.0)
    contrib_str = f"+{contribution:5.1f}" if contribution else "     -"
    weight = info.get("weight", 0.0)
    return (
        f"  {name:<18} {bar(info['score'])} {info['score']:5.1f}"
        f"  w {weight * 100:4.1f}%  {contrib_str}  {mark}"
    )


def render(diag: dict) -> str:
    """The full dashboard panel as a string (also unit-testable without a terminal)."""
    lines: list[str] = []
    ts = diag["timestamp"].split("T")[-1][:8]
    conf = diag["confidence"]
    action = diag["action"]
    profile = diag.get("profile") or "default"

    lines.append("=" * 78)
    lines.append(f" TRADING DECISION ENGINE — {ts}   profile: {profile}   decision: {action}")
    lines.append("=" * 78)
    lines.append(f"  BUY  confidence   {bar(conf['buy'])} {conf['buy']:5.1f}%")
    lines.append(f"  SELL confidence   {bar(conf['sell'])} {conf['sell']:5.1f}%")
    lines.append(f"  HOLD              {bar(conf['hold'])} {conf['hold']:5.1f}%")
    if conf.get("exit"):
        lines.append(f"  EXIT confidence   {bar(conf['exit'])} {conf['exit']:5.1f}%")
    lines.append("-" * 78)

    lines.append(f"  {'ENGINE':<18} {'SCORE':^{BAR_WIDTH}} {'':>5}  {'WEIGHT':>7} {'CONTRIB':>7}")
    display_order = (
        ("Trend", "trend"), ("Market Structure", "market_structure"),
        ("Support/Resist", "support_resistance"), ("Premium Momentum", "premium_momentum"),
        ("Breakout", "breakout"), ("Market Strength", "market_strength"),
        ("Option Selection", "option_selection"), ("Volatility", "volatility"),
        ("Trading Rules", "trading_rules"), ("Risk", "risk"), ("Signal Stability", "signal_stability"),
    )
    for label, key in display_order:
        info = diag["engines"].get(key)
        if info:
            lines.append(_fmt_engine_line(label, info))

    # Key live numbers the operator actually watches
    eng = diag["engines"]
    mom = eng.get("premium_momentum", {})
    sr = eng.get("support_resistance", {})
    stab = eng.get("signal_stability", {})
    opt = eng.get("option_selection", {})
    lines.append("-" * 78)
    lines.append(
        f"  momentum v {mom.get('velocity', 0):+.2f}/s a {mom.get('acceleration', 0):+.2f} cons {mom.get('consistency_pct', 0):.0f}%"
        f"   room: res {sr.get('distance_to_resistance', '-')} sup {sr.get('distance_to_support', '-')}"
        f"   stability {stab.get('elapsed_seconds', 0):.1f}/{stab.get('required_seconds', 0):.1f}s"
    )
    if opt.get("best_ce") or opt.get("best_pe"):
        lines.append(f"  strikes: CE {opt.get('best_ce') or '-'} @ {opt.get('ce_premium') or '-'}   PE {opt.get('best_pe') or '-'} @ {opt.get('pe_premium') or '-'}")

    # Stage 1 + Stage 2
    s1, s2 = diag["stage1"], diag["stage2"]
    lines.append("-" * 78)
    gate_bits = []
    for gate, chk in s1["checks"].items():
        if not chk["enabled"]:
            continue
        gate_bits.append(f"{gate.replace('_', ' ')}: {'PASS' if chk['passed'] else 'FAIL'}")
    lines.append(f"  STAGE 1 [{'PASS' if s1['passed'] else 'FAIL'}]  " + " | ".join(gate_bits))
    if not s1["passed"]:
        for gate in s1["failed_checks"]:
            chk = s1["checks"].get(gate, {})
            lines.append(f"    ✗ {gate.replace('_', ' '):<18} actual: {chk.get('actual')}   required: {chk.get('required')}")
    if s2["evaluated"]:
        lines.append(
            f"  STAGE 2  buy {s2['buy_score']:.0f}/{s2['required_buy']:.0f}  sell {s2['sell_score']:.0f}/{s2['required_sell']:.0f}"
            f"  confidence {s2['confidence']:.0f}  quality {s2['trade_quality']:.0f}  agreement {s2['engine_agreement']} ({s2['engine_agreement_pct']:.0f}%)"
        )
    else:
        lines.append("  STAGE 2  not evaluated (Stage-1 rejected)")

    lines.append("-" * 78)
    lines.append(f"  FINAL: {action}")
    for reason in diag["final"]["reasons"][:6]:
        lines.append(f"    • {reason}")
    lines.append("=" * 78)
    return "\n".join(lines)


def render_trade_panel(panel: dict) -> str:
    """In-trade view: position, live P&L, and which exit triggers are being watched."""
    lines = []
    ts = panel["timestamp"].split("T")[-1][:8]
    pnl = panel["pnl"]
    pnl_str = f"₹{pnl:+,.2f}"
    lines.append("=" * 78)
    lines.append(f" IN TRADE — {ts}   {panel['instrument']}   {panel['lots']} lot(s)")
    lines.append("=" * 78)
    lines.append(f"  entry {panel['entry_price']:.2f}   now {panel['current_price']:.2f}   P&L {pnl_str}   {panel['time_in_trade_seconds']:.0f}s in trade")
    lines.append(f"  premium range seen: {panel['lowest_premium']:.2f} – {panel['highest_premium']:.2f}")
    lines.append(f"  exit triggers armed: reversal · momentum loss · failed breakout · level failure · risk · close buffer")
    lines.append("=" * 78)
    return "\n".join(lines)


class ConsoleDashboard:
    """Throttled in-place terminal renderer. update() is the Orchestrator's
    on_diagnostics callback; safe to call on every cycle. When a SessionStatistics
    aggregator is attached, a one-line session strip (signals, rejects, trades, top
    blocking gate) is appended under every panel.
    """

    def __init__(self, refresh_seconds: float = 1.0, stream=None, stats=None) -> None:
        self._refresh_seconds = refresh_seconds
        self._stream = stream if stream is not None else sys.stdout
        self._stats = stats  # SessionStatistics | None
        self._last_draw = 0.0
        self._enabled = hasattr(self._stream, "isatty") and self._stream.isatty()

    def update(self, diag: dict) -> None:
        if self._stats is not None:
            self._stats.update(diag)  # aggregate every event, even between redraws
        if not self._enabled:
            return
        now = time.monotonic()
        # Trades must never wait for the next refresh window.
        force = diag.get("action") in ("BUY", "SELL", "EXIT") or diag.get("type") in ("trade_panel", "trade_closed")
        if not force and (now - self._last_draw) < self._refresh_seconds:
            return
        if diag.get("type") == "trade_closed":
            return  # aggregated above; the next cycle panel reflects it
        self._last_draw = now
        panel = render_trade_panel(diag) if diag.get("type") == "trade_panel" else render(diag)
        if self._stats is not None:
            from .session_statistics import render_stats_strip

            panel += "\n" + render_stats_strip(self._stats.to_dict())
        self._stream.write(_CLEAR + panel + "\n")
        self._stream.flush()
