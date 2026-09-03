"""Make the integration package importable without Home Assistant.

tests/stubs contains a minimal `homeassistant` stub package so that
importing `smart_battery_pilot` (whose __init__ pulls in HA modules)
works in a plain pytest environment.
"""

from pathlib import Path
import sys

_BASE = Path(__file__).parent
sys.path.insert(0, str(_BASE / "stubs"))
sys.path.insert(0, str(_BASE.parent / "custom_components"))
