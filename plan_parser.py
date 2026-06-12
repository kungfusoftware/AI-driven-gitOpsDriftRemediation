"""
Terraform Plan Parser
----------------------
Parses terraform plan JSON output into a clean, concise summary dict
that is safe to send to Azure OpenAI (within token limits).
"""

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("drift-agent.parser")

MAX_BEFORE_AFTER_VALUES = 20   # Cap attribute before/after pairs per resource


class TerraformPlanParser:
    def __init__(self, plan_json_path: str, plan_text_path: str):
        with open(plan_json_path) as f:
            self._plan: dict = json.load(f)
        with open(plan_text_path) as f:
            self._plan_text: str = f.read()

    def build_summary(self) -> dict[str, Any]:
        """Build a token-efficient drift summary for AI consumption."""
        resource_changes = self._plan.get("resource_changes", [])
        output_changes   = self._plan.get("output_changes",   {})

        adds, changes, destroys, replacements, drifted = [], [], [], [], []

        for rc in resource_changes:
            actions = rc.get("change", {}).get("actions", [])
            if "no-op" in actions or "read" in actions:
                continue

            entry = self._build_resource_entry(rc)

            if actions == ["create"]:
                adds.append(entry)
            elif actions == ["delete"]:
                destroys.append(entry)
            elif actions == ["update"]:
                changes.append(entry)
            elif set(actions) == {"delete", "create"}:
                replacements.append(entry)

        # Drift (actual infrastructure state differs from plan)
        for dr in self._plan.get("resource_drift", []):
            drifted.append({
                "address":  dr.get("address"),
                "type":     dr.get("type"),
                "provider": dr.get("provider_name"),
                "actions":  dr.get("change", {}).get("actions", []),
            })

        return {
            "terraform_version": self._plan.get("terraform_version", "unknown"),
            "format_version":    self._plan.get("format_version", "unknown"),
            "counts": {
                "add":         len(adds),
                "change":      len(changes),
                "destroy":     len(destroys),
                "replace":     len(replacements),
                "drift":       len(drifted),
            },
            "adds":         adds[:10],        # Cap to avoid token overflow
            "changes":      changes[:15],
            "destroys":     destroys[:10],
            "replacements": replacements[:5],
            "drifted":      drifted[:10],
            "output_changes": list(output_changes.keys()),
            "plan_text_excerpt": self._plan_text[:3000],   # First 3k chars for context
        }

    # ── Private ────────────────────────────────────────────────────────────────

    @staticmethod
    def _build_resource_entry(rc: dict) -> dict:
        """Extract the key fields from a resource_change entry."""
        change   = rc.get("change", {})
        before   = change.get("before") or {}
        after    = change.get("after")  or {}

        # Find attributes that actually changed
        changed_attrs = {}
        all_keys = set(before.keys()) | set(after.keys())
        for key in list(all_keys)[:MAX_BEFORE_AFTER_VALUES]:
            b = before.get(key)
            a = after.get(key)
            if b != a:
                changed_attrs[key] = {"before": _sanitize(b), "after": _sanitize(a)}

        return {
            "address":      rc.get("address"),
            "type":         rc.get("type"),
            "name":         rc.get("name"),
            "provider":     rc.get("provider_name"),
            "actions":      change.get("actions", []),
            "changed_attrs": changed_attrs,
        }


def _sanitize(value: Any, max_len: int = 120) -> Any:
    """Truncate long strings to prevent token explosion."""
    if isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "…"
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in list(value.items())[:10]}
    if isinstance(value, list):
        return [_sanitize(v) for v in value[:5]]
    return value
