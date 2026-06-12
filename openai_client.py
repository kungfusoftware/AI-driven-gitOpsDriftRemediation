"""
Azure OpenAI Client
-------------------
Handles all interactions with Azure OpenAI — drift analysis and
Terraform HCL remediation code generation.
"""

import json
import logging
import os
import re
from typing import Any

from openai import AzureOpenAI

log = logging.getLogger("drift-agent.openai")

# ── System Prompts ─────────────────────────────────────────────────────────────
ANALYSIS_SYSTEM_PROMPT = """
You are an expert Azure Infrastructure Engineer and Terraform specialist.
Your job is to analyse Terraform drift in an Azure Staging environment.

When given a drift summary (resource changes), you must respond ONLY with a
valid JSON object — no markdown, no preamble — using this exact schema:

{
  "risk_level": "LOW | MEDIUM | HIGH | CRITICAL",
  "summary": "Plain-English summary of what drifted and why it matters",
  "root_cause": "Likely cause of the drift (manual change, missing import, etc.)",
  "recommended_actions": ["action 1", "action 2"],
  "security_impact": "Description or 'None'",
  "cost_impact": "Description or 'None'",
  "safe_to_auto_remediate": true | false
}

Risk levels:
  LOW      – minor tag/metadata changes, no functional impact
  MEDIUM   – config drift, scaling changes, non-breaking
  HIGH     – security group changes, role assignments, storage changes
  CRITICAL – destructive changes (deletes), IAM, network topology changes
""".strip()

REMEDIATION_SYSTEM_PROMPT = """
You are an expert Terraform & Azure IaC engineer performing automated drift remediation.

Given:
1. A Terraform drift summary (what changed in Azure vs. Terraform code)
2. An AI analysis of that drift

Generate the MINIMUM Terraform HCL changes needed to reconcile the code with
the actual Azure state — i.e., bring the Terraform code IN SYNC with reality.

Rules:
- Only modify existing .tf files (no new files unless absolutely necessary)
- Never remove resources unless the drift analysis says "safe_to_auto_remediate": true
  AND the change is a deletion
- Preserve all existing comments and formatting style
- For CRITICAL or HIGH risk items, add a TODO comment instead of auto-fixing
- File paths must be relative to the repository root

Respond ONLY with valid JSON — no markdown, no explanation — using this schema:

{
  "file_changes": [
    {
      "path": "terraform/staging/main.tf",
      "description": "What changed and why",
      "content": "<full updated file content as a string>"
    }
  ],
  "skipped_changes": [
    {
      "resource": "resource_type.resource_name",
      "reason": "Why this was skipped (e.g. CRITICAL risk, manual review required)"
    }
  ],
  "verification_commands": ["terraform validate", "terraform plan -var-file=staging.tfvars"]
}

If no safe changes can be made, return:
{ "file_changes": [], "skipped_changes": [...], "verification_commands": [] }
""".strip()


class AzureOpenAIClient:
    """Wraps Azure OpenAI completions for drift analysis and remediation."""

    def __init__(self):
        self.client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version="2024-02-01",
        )
        self.deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]
        log.info("Azure OpenAI client initialised — deployment: %s", self.deployment)

    # ── Public Methods ─────────────────────────────────────────────────────────

    def analyze_drift(self, plan_summary: dict, arg_report: dict | None = None) -> dict:
        """Send plan summary (and optional ARG findings) to AI for risk analysis."""
        log.info("Requesting drift analysis from Azure OpenAI …")

        arg_section = ""
        if arg_report and arg_report.get("total_findings", 0) > 0:
            # Trim findings to avoid token overflow — top 20 by severity
            sev_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
            top_findings = sorted(
                arg_report.get("findings", []),
                key=lambda f: sev_order.get(f.get("severity", "LOW"), 9),
            )[:20]
            trimmed = {
                "queried_at":          arg_report.get("queried_at"),
                "resource_group":      arg_report.get("resource_group"),
                "total_findings":      arg_report.get("total_findings"),
                "highest_severity":    arg_report.get("highest_severity"),
                "summary_by_category": arg_report.get("summary_by_category"),
                "top_findings":        top_findings,
            }
            arg_section = (
                "\n\n## Azure Resource Graph Live Scan Findings\n"
                f"```json\n{json.dumps(trimmed, indent=2)}\n```\n"
                "The ARG findings represent the **live Azure control plane state** "
                "queried seconds ago — treat these as ground truth for what exists in Azure."
            )

        user_content = (
            "Analyse the following Terraform drift detected in Azure Staging:\n\n"
            f"## Terraform Plan Summary\n```json\n{json.dumps(plan_summary, indent=2)}\n```"
            f"{arg_section}"
        )

        response = self._chat(
            system=ANALYSIS_SYSTEM_PROMPT,
            user=user_content,
            max_tokens=1500,
            temperature=0.1,
        )

        analysis = self._parse_json_response(response, "drift analysis")
        log.info("Analysis complete — risk: %s", analysis.get("risk_level"))
        return analysis

    def generate_remediation(
        self,
        plan_summary: dict,
        analysis: dict,
        arg_report: dict | None = None,
    ) -> dict:
        """Generate Terraform HCL remediation code for the detected drift."""
        log.info("Requesting remediation code from Azure OpenAI …")

        # Don't auto-remediate CRITICAL changes
        if analysis.get("risk_level") == "CRITICAL":
            log.warning("CRITICAL risk detected — skipping auto-remediation code generation.")
            return {
                "file_changes": [],
                "skipped_changes": [
                    {
                        "resource": "*",
                        "reason": "CRITICAL risk level — requires manual review before remediation",
                    }
                ],
                "verification_commands": [],
            }

        arg_section = ""
        if arg_report and arg_report.get("total_findings", 0) > 0:
            arg_section = (
                "\n\n## ARG Live Findings (use these as the authoritative live state)\n"
                f"```json\n{json.dumps(arg_report.get('top_findings', arg_report.get('findings', []))[:10], indent=2)}\n```"
            )

        user_content = (
            "## Drift Summary\n"
            f"```json\n{json.dumps(plan_summary, indent=2)}\n```\n\n"
            "## AI Analysis\n"
            f"```json\n{json.dumps(analysis, indent=2)}\n```"
            f"{arg_section}\n\n"
            "Generate the Terraform HCL remediation changes."
        )

        response = self._chat(
            system=REMEDIATION_SYSTEM_PROMPT,
            user=user_content,
            max_tokens=4096,
            temperature=0.05,   # Very low temp for code generation
        )

        remediation = self._parse_json_response(response, "remediation")
        log.info(
            "Remediation generated — %d file(s) to change, %d skipped",
            len(remediation.get("file_changes", [])),
            len(remediation.get("skipped_changes", [])),
        )
        return remediation

    # ── Private Helpers ────────────────────────────────────────────────────────

    def _chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.1,
    ) -> str:
        """Execute a chat completion and return the raw text response."""
        try:
            completion = self.client.chat.completions.create(
                model=self.deployment,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return completion.choices[0].message.content or ""
        except Exception as exc:
            log.error("Azure OpenAI API call failed: %s", exc)
            raise

    @staticmethod
    def _parse_json_response(raw: str, context: str) -> dict[str, Any]:
        """Strip markdown fences and parse JSON from AI response."""
        # Remove ```json ... ``` or ``` ... ``` fences if present
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            log.error("Failed to parse AI JSON response (%s): %s\nRaw:\n%s", context, exc, raw[:500])
            # Return a safe fallback so the pipeline doesn't crash
            return {
                "risk_level": "HIGH",
                "summary": f"AI response parse error — raw output stored in logs ({context})",
                "recommended_actions": ["Manual review required"],
                "safe_to_auto_remediate": False,
                "file_changes": [],
                "skipped_changes": [],
            }
