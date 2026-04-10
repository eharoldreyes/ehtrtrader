"""
strategies/__init__.py  –  Strategy registry.

To add a new strategy:
  1. Create  strategies/your_strategy_name.py
  2. Expose  PARAMS list  and  run(symbol, shares, duration_days, params) in it
  3. Import and register it below
"""

from strategies import trailing_stop_loss

# Registry maps strategy name → module
# Each module must expose:
#   PARAMS : list[dict]   — parameter schema for the interactive menu
#                           must include "initial_shares" and "duration" keys
#   run(symbol, params)   — entry point
REGISTRY: dict[str, object] = {
    "trailing_stop_loss": trailing_stop_loss,
}


def get(name: str):
    """Return the strategy module for the given name."""
    if name not in REGISTRY:
        available = ", ".join(REGISTRY.keys())
        raise ValueError(
            f"Unknown strategy '{name}'.\n"
            f"Available: {available}"
        )
    return REGISTRY[name]


def list_strategies() -> list[str]:
    return list(REGISTRY.keys())
