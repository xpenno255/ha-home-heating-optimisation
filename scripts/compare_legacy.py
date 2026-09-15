"""Compare engines on one private legacy history without publishing household data.

Run with the project venv: python -m scripts.compare_legacy --help
The supplied legacy component code is imported and must be a trusted checkout.
"""

import argparse
import importlib
import json
import sys
import types
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from custom_components.home_heating_optimisation.analytics.analyzer import compute_analytics


def compare(legacy_component, history_file):
    package = types.ModuleType("legacy_comparison")
    package.__path__ = [str(legacy_component.resolve())]
    sys.modules[package.__name__] = package
    legacy = importlib.import_module("legacy_comparison.analyzer")
    payload = json.loads(history_file.read_text())
    payload = payload.get("data", payload)
    points = payload["observations"]
    zones = sorted({z for p in points for z in p["zones"]})
    now = datetime.fromtimestamp(max(p["time"] for p in points), timezone.utc)
    differences = {}
    for days in (1, 3, 7, 14):
        old = asdict(
            legacy.compute_analytics(points, zones, days, now=now, timezone_name="Europe/London")
        )
        new = asdict(compute_analytics(points, zones, days, now=now, timezone_name="Europe/London"))
        changed = {}
        for zone in zones:
            for key, value in old["zone_stats"][zone].items():
                if value != new["zone_stats"][zone][key]:
                    changed[key] = changed.get(key, 0) + 1
        if old["system"] != new["system"]:
            changed["system"] = 1
        differences[str(days)] = changed
    return {
        "observations": len(points),
        "rooms": len(zones),
        "differing_fields_by_window_days": differences,
        "scope": "identical stored observations; ingestion changes tested separately",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-component", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args.legacy_component, args.history), indent=2))
