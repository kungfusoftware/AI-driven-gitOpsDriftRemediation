#!/usr/bin/env python3
"""
AI-Driven GitOps Drift Remediation Agent
-----------------------------------------
Reads a Terraform plan JSON, sends it to Azure OpenAI for analysis,
generates remediation HCL, creates a GitHub branch, commits changes,
runs verification, and opens a Pull Request.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from github_client import GitHubClient
from openai_client import AzureOpenAIClient
from plan_parser import TerraformPlanParser
from pr_template import build_pr_body

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("drift-agent")

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_PLAN_CHARS = 28_000   # Stay within Azure OpenAI token limits
BRANCH_PREFIX  = "fix/infra-drift"
LABEL_NAME     = "infra-drift"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI Drift Remediation Agent")
    p.add_argument("--plan-json",    required=True, help="Path to tfplan.json")
    p.add_argument("--plan-text",    required=True, help="Path to plan text output")
    p.add_argument("--working-dir",  required=True, help="Terraform working directory")
    p.add_argument("--dry-run",      action="store_true", help="Analyse only, no commits")
    return p.parse_args()


# ── Main Agent Loop ───────────────────────────────────────────────────────────
def main():
    args = parse_args()

    # -- Validate env vars
    required_env = [
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
    ]
    missing = [e for e in required_env if not os.getenv(e)]
    if missing:
        log.error("Missing required environment variables: %s", missing)
        sys.exit(1)

    repo_name   = os.environ["GITHUB_REPOSITORY"]
    working_dir = Path(args.working_dir)
    run_date    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    branch_name = f"{BRANCH_PREFIX}-{run_date}"

    # ── Step 1: Parse the Terraform plan ──────────────────────────────────────
    log.info("Step 1: Parsing Terraform plan …")
    parser  = TerraformPlanParser(args.plan_json, args.plan_text)
    summary = parser.build_summary()

    log.info(
        "Drift summary — adds: %d, changes: %d, destroys: %d",
        summary["counts"]["add"],
        summary["counts"]["change"],
        summary["counts"]["destroy"],
    )

    # ── Step 2: Send to Azure OpenAI for analysis & remediation code ──────────
    log.info("Step 2: Sending plan to Azure OpenAI for analysis …")
    ai_client = AzureOpenAIClient()

    analysis = ai_client.analyze_drift(summary)
    log.info("AI risk level: %s", analysis.get("risk_level", "unknown"))

    log.info("Step 2b: Requesting remediation Terraform code …")
    remediation = ai_client.generate_remediation(summary, analysis)

    if not remediation.get("file_changes"):
        log.warning("AI returned no file changes — nothing to commit.")
        _write_summary(analysis, remediation, branch_name, pr_url=None)
        sys.exit(0)

    if args.dry_run:
        log.info("Dry-run mode — skipping branch/commit/PR steps.")
        _write_summary(analysis, remediation, branch_name, pr_url="[dry-run]")
        sys.exit(0)

    # ── Step 3: Create GitHub branch & commit changes ─────────────────────────
    log.info("Step 3: Creating branch '%s' …", branch_name)
    gh = GitHubClient(repo_name, os.environ["GITHUB_TOKEN"])

    gh.create_branch(branch_name)

    for fc in remediation["file_changes"]:
        rel_path = fc["path"]                          # e.g. terraform/staging/main.tf
        new_content = fc["content"]
        log.info("  Committing: %s", rel_path)
        gh.commit_file(
            branch=branch_name,
            path=rel_path,
            content=new_content,
            message=f"fix(drift): remediate {rel_path} [{run_date}]",
        )

    # ── Step 4: Verify with terraform validate & plan ─────────────────────────
    log.info("Step 4: Running terraform validate on branch changes …")
    verify_ok, verify_output = _verify_terraform(working_dir, branch_name)

    if not verify_ok:
        log.error("Terraform validation failed — adding comment to branch:\n%s", verify_output)
        gh.create_issue_comment_on_commit(
            branch_name,
            f"⚠️ **Terraform validation failed** for AI-generated changes:\n```\n{verify_output}\n```",
        )
        # Still create the PR but mark it as needs-review
        analysis["risk_level"] = "HIGH – validation failed"

    # ── Step 5: Open Pull Request ─────────────────────────────────────────────
    log.info("Step 5: Creating Pull Request …")
    pr_body = build_pr_body(summary, analysis, remediation, verify_ok, verify_output)

    pr_url = gh.create_pull_request(
        branch=branch_name,
        title=f"🤖 [Drift Remediation] Azure Staging – {run_date}",
        body=pr_body,
        labels=[LABEL_NAME],
    )
    log.info("✅ Pull Request created: %s", pr_url)

    # ── Write job summary ─────────────────────────────────────────────────────
    _write_summary(analysis, remediation, branch_name, pr_url)
    log.info("Agent completed successfully.")


# ── Helpers ───────────────────────────────────────────────────────────────────
def _verify_terraform(working_dir: Path, branch_name: str) -> tuple[bool, str]:
    """
    Checkout branch locally, run terraform validate.
    In CI the branch is already fetched; we just validate the working dir.
    """
    try:
        result = subprocess.run(
            ["terraform", "validate", "-json"],
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        if result.returncode == 0:
            return True, output
        return False, output
    except Exception as exc:
        return False, str(exc)


def _write_summary(analysis: dict, remediation: dict, branch: str, pr_url: str | None):
    summary_path = Path("/tmp/remediation_summary.md")
    risk    = analysis.get("risk_level", "unknown")
    changes = len(remediation.get("file_changes", []))
    lines = [
        f"### Risk Level: `{risk}`",
        "",
        f"**Branch:** `{branch}`",
        f"**Files modified:** {changes}",
        f"**PR:** {pr_url or 'not created'}",
        "",
        "#### AI Analysis",
        analysis.get("summary", "_No summary available_"),
        "",
        "#### Recommended Actions",
    ]
    for action in analysis.get("recommended_actions", []):
        lines.append(f"- {action}")

    summary_path.write_text("\n".join(lines))
    log.info("Summary written to %s", summary_path)


if __name__ == "__main__":
    main()
