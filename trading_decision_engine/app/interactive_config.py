"""Interactive startup prompts (mode / profile / index / expiry / lots / premium range /
order validation) — the same UX pattern as
QA_PASS_env_sample_test_groww_option_trading_final_bot-DO_NOT_DELETE.py's
prompt_index_selection/prompt_expiry_selection/prompt_lots/prompt_price_range, reused
here as run.py's default flow so a bare `python -m trading_decision_engine.app.run`
asks for everything. Each still has a CLI-flag escape hatch (see run.py) for
scripted/automated runs where a prompt would block.
"""

from __future__ import annotations

from .broker.instrument_master import InstrumentMaster
from .config.strategy import PROFILES_DIR

INDEX_OPTIONS = {"1": "NIFTY", "2": "BANKNIFTY", "3": "SENSEX", "4": "FINNIFTY"}
MODE_OPTIONS = {"1": "live", "2": "shadow", "3": "replay"}


def prompt_mode() -> str:
    while True:
        print("\nSelect Mode:")
        print("  (1) live    — real orders, real money")
        print("  (2) shadow  — real market data, SIMULATED orders (recommended first)")
        print("  (3) replay  — historical backtest, no live connection")
        choice = input("Enter choice (1-3) or type mode name: ").strip().lower()
        if choice in MODE_OPTIONS:
            mode = MODE_OPTIONS[choice]
        elif choice in MODE_OPTIONS.values():
            mode = choice
        else:
            print("Invalid choice, try again.")
            continue
        if mode == "live":
            confirm = input("LIVE mode places REAL orders with REAL money. Type 'yes' to confirm: ").strip().lower()
            if confirm != "yes":
                continue
        return mode


def prompt_profile() -> str | None:
    profiles = sorted(p.stem for p in PROFILES_DIR.glob("*.json")) if PROFILES_DIR.exists() else []
    if not profiles:
        return None
    while True:
        print("\nSelect Strategy Profile:")
        print("  (0) none — use strategy.json as-is")
        for i, name in enumerate(profiles, start=1):
            print(f"  ({i}) {name}")
        choice = input(f"Enter choice (0-{len(profiles)}) or type profile name: ").strip().lower()
        if choice in ("0", "none", ""):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            return profiles[int(choice) - 1]
        if choice in profiles:
            return choice
        print("Invalid choice, try again.")


def prompt_validate_orders() -> bool:
    while True:
        choice = input(
            "\nValidate orders? (y = confirm real fill price/qty before trusting a trade — recommended; n = trust immediate response) [Y/n]: "
        ).strip().lower()
        if choice in ("", "y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print("Enter 'y' or 'n'.")


def prompt_index_selection() -> str:
    while True:
        print("\nSelect Index:")
        for key, name in INDEX_OPTIONS.items():
            print(f"  ({key}) {name}")
        choice = input("Enter choice (1-4) or type index name: ").strip().upper()
        if choice in INDEX_OPTIONS:
            return INDEX_OPTIONS[choice]
        if choice in INDEX_OPTIONS.values():
            return choice
        print("Invalid choice, try again.")


def prompt_expiry_selection(instruments: InstrumentMaster, index: str) -> str:
    expiries = instruments.expiries_for(index)
    if not expiries:
        return input(
            f"No expiries found for {index} in instrument.csv (it may be stale). Enter expiry date (YYYY-MM-DD): "
        ).strip()

    current_expiry = expiries[0]
    next_expiry = expiries[1] if len(expiries) > 1 else expiries[0]
    while True:
        choice = input(
            f"\nChoose expiry for {index} — (c)urrent [{current_expiry}] or (n)ext [{next_expiry}]: "
        ).strip().lower()
        if choice in ("c", "current", ""):
            return current_expiry
        if choice in ("n", "next"):
            return next_expiry
        print("Invalid choice, enter 'c' or 'n'.")


def prompt_lots() -> int:
    while True:
        raw = input("\nEnter number of lots to trade: ").strip()
        try:
            lots = int(raw)
        except ValueError:
            print("Enter a valid integer.")
            continue
        if lots > 0:
            return lots
        print("Lots must be a positive integer.")


def prompt_premium_range() -> tuple[float, float]:
    while True:
        try:
            min_p = float(input("\nEnter MIN premium price to trade: ").strip())
            max_p = float(input("Enter MAX premium price to trade: ").strip())
        except ValueError:
            print("Enter valid numbers.")
            continue
        if min_p < max_p:
            return min_p, max_p
        print("MIN must be less than MAX.")
