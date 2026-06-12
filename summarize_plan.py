#!/usr/bin/env python3
"""
summarize_plan.py
-----------------
Lightweight script called from GitHub Actions shell step.
Prints a one-line drift summary to stdout (captured as step output).
"""

import json
import sys


def main():
    if len(sys.argv) < 2:
        print("usage: summarize_plan.py <tfplan.json>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        plan = json.load(f)

    adds     = sum(1 for rc in plan.get("resource_changes", []) if rc.get("change", {}).get("actions") == ["create"])
    changes  = sum(1 for rc in plan.get("resource_changes", []) if rc.get("change", {}).get("actions") == ["update"])
    destroys = sum(1 for rc in plan.get("resource_changes", []) if rc.get("change", {}).get("actions") == ["delete"])
    drifts   = len(plan.get("resource_drift", []))

    print(f"adds={adds} changes={changes} destroys={destroys} drifted={drifts}")


if __name__ == "__main__":
    main()
