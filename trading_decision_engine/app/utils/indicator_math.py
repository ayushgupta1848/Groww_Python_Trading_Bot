"""Pure math ported from reference/tw_all_in_one_indicator.pine (EHMA/EMA/WMA, pivot
high/low, valuewhen). No broker, no I/O, no plotting/coloring logic — computation only,
per docs/DESIGN.md §3/§7 ("Preserve Support/Resistance Logic... translate only its
mathematical logic").

All series functions take a plain sequence of floats (oldest-first) and return a list of
the same length, aligned index-for-index, using None wherever Pine would return `na`
(insufficient history).
"""

from __future__ import annotations

import math
from typing import Sequence


def sma(values: Sequence[float], length: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i + 1 < length:
            continue
        window = values[i + 1 - length : i + 1]
        out[i] = sum(window) / length
    return out


def wma(values: Sequence[float], length: int) -> list[float | None]:
    """Pine's ta.wma: linearly increasing weights, heaviest on the most recent bar."""
    denom = length * (length + 1) / 2
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i + 1 < length:
            continue
        window = values[i + 1 - length : i + 1]
        weighted = sum(v * (idx + 1) for idx, v in enumerate(window))
        out[i] = weighted / denom
    return out


def ema(values: Sequence[float], length: int) -> list[float | None]:
    """Pine's ta.ema: SMA-seeded, then recursive EMA with alpha = 2 / (length + 1)."""
    alpha = 2.0 / (length + 1)
    out: list[float | None] = [None] * len(values)
    seed_idx = length - 1
    if seed_idx >= len(values):
        return out
    seed_window = values[0:length]
    prev = sum(seed_window) / length
    out[seed_idx] = prev
    for i in range(seed_idx + 1, len(values)):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def ehma(values: Sequence[float], length: int) -> list[float | None]:
    """EHMA(src, length) = ema(2*ema(src, length) - ema(src, length), round(sqrt(length))).

    Ported literally from the .pine formula (both operands of the subtraction are the
    same ema(src, length) call in the original script) rather than simplified, per the
    "translate only its mathematical logic, exactly" instruction.
    """
    e = ema(values, length)
    combined: list[float] = [
        (2 * v - v) if v is not None else 0.0 for v in e
    ]
    # Only feed the outer ema the portion of `combined` from the first non-None sample
    # onward; earlier slots are meaningless (0.0 placeholders) and must stay None.
    first_valid = next((i for i, v in enumerate(e) if v is not None), None)
    if first_valid is None:
        return [None] * len(values)
    sqrt_len = round(math.sqrt(length))
    inner = combined[first_valid:]
    outer = ema(inner, sqrt_len)
    out: list[float | None] = [None] * len(values)
    out[first_valid : first_valid + len(outer)] = outer
    return out


def pivot_high_flags(values: Sequence[float], left: int, right: int) -> list[bool]:
    """True at index p if values[p] is a confirmed Pine pivothigh: at least as high as
    every bar in the `left` bars before it, and STRICTLY higher than every bar in the
    `right` bars after it. Only confirmable once `right` bars after p exist.

    The two sides are deliberately asymmetric, matching real Pine `pivothigh`:
    - A tie against an earlier (left) bar does not disqualify — otherwise a tied
      plateau would produce NO pivot at all (a naive strict-both-sides comparison
      silently drops every plateau peak, since each tied bar disqualifies the other).
    - A tie against a later (right) bar DOES disqualify — so exactly ONE bar of a
      plateau confirms (the last tied bar), never several. Confirming every tied bar
      would double-count a single peak as multiple swing points, which downstream
      structure analysis reads as a phantom double-top (two "consecutive swing highs"
      at literally identical values).
    - A perfectly flat window confirms nothing (the strict right side can never pass).
    """
    n = len(values)
    flags = [False] * n
    for p in range(n):
        if p - left < 0 or p + right >= n:
            continue
        center = values[p]
        if all(center >= values[i] for i in range(p - left, p)) and all(center > values[i] for i in range(p + 1, p + right + 1)):
            flags[p] = True
    return flags


def pivot_low_flags(values: Sequence[float], left: int, right: int) -> list[bool]:
    """True at index p if values[p] is a confirmed Pine pivotlow: at least as low as
    every bar in the `left` bars before it, and STRICTLY lower than every bar in the
    `right` bars after it. See pivot_high_flags for why the sides are asymmetric.
    """
    n = len(values)
    flags = [False] * n
    for p in range(n):
        if p - left < 0 or p + right >= n:
            continue
        center = values[p]
        if all(center <= values[i] for i in range(p - left, p)) and all(center < values[i] for i in range(p + 1, p + right + 1)):
            flags[p] = True
    return flags


def value_when(
    condition: Sequence[bool], source: Sequence[float | None], occurrence: int
) -> float | None:
    """Pine's valuewhen(condition, source, occurrence): the value of `source` at the
    occurrence-th most recent index (searching backward from the end) where `condition`
    was True. occurrence=0 is the most recent match. Returns None if there aren't enough
    matches (Pine's `na`).
    """
    matches = [i for i, c in enumerate(condition) if c]
    if occurrence >= len(matches):
        return None
    idx = matches[-(occurrence + 1)]
    return source[idx] if idx < len(source) else None


def shifted(values: Sequence[float], offset: int) -> list[float | None]:
    """Pine's `close[offset]`: value `offset` bars before the current bar, aligned so
    shifted(values, offset)[i] == values[i - offset] (None where i - offset < 0).
    """
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i - offset >= 0:
            out[i] = values[i - offset]
    return out
