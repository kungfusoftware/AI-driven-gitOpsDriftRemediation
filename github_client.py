"""
GitHub Client
-------------
Handles all GitHub REST API operations:
  - Create branches
  - Commit file changes
  - Create Pull Requests
  - Add labels
"""

import base64
import logging
import os
from typing import Optional

from github import Github, GithubException, InputGitAuthor

log = logging.getLogger("drift-agent.github")

BOT_AUTHOR = InputGitAuthor(
    name="Drift Remediation Bot",
    email="drift-bot@github-actions.noreply.github.com",
)


class GitHubClient:
    """Thin wrapper around PyGitHub for branch / commit / PR operations."""

    def __init__(self, repo_name: str, token: str):
        self._gh   = Github(token)
        self._repo = self._gh.get_repo(repo_name)
        log.info("GitHub client connected — repo: %s", repo_name)

    # ── Branch ────────────────────────────────────────────────────────────────

    def create_branch(self, branch_name: str, base_branch: str = "main") -> None:
        """Create a new branch from base_branch. Idempotent — skips if exists."""
        try:
            self._repo.get_branch(branch_name)
            log.info("Branch '%s' already exists — reusing.", branch_name)
            return
        except GithubException:
            pass  # branch does not exist — create it

        base_sha = self._repo.get_branch(base_branch).commit.sha
        self._repo.create_git_ref(
            ref=f"refs/heads/{branch_name}",
            sha=base_sha,
        )
        log.info("Branch '%s' created from '%s' (%s)", branch_name, base_branch, base_sha[:8])

    # ── Files ─────────────────────────────────────────────────────────────────

    def commit_file(
        self,
        branch: str,
        path: str,
        content: str,
        message: str,
    ) -> None:
        """
        Create or update a file on the given branch.
        content should be the full new file content (plain string).
        """
        content_bytes = content.encode("utf-8")

        try:
            existing = self._repo.get_contents(path, ref=branch)
            # Update existing file
            self._repo.update_file(
                path=path,
                message=message,
                content=content_bytes,
                sha=existing.sha,
                branch=branch,
                author=BOT_AUTHOR,
                committer=BOT_AUTHOR,
            )
            log.info("Updated file: %s", path)
        except GithubException as exc:
            if exc.status == 404:
                # Create new file
                self._repo.create_file(
                    path=path,
                    message=message,
                    content=content_bytes,
                    branch=branch,
                    author=BOT_AUTHOR,
                    committer=BOT_AUTHOR,
                )
                log.info("Created file: %s", path)
            else:
                log.error("GitHub file commit failed (%s): %s", path, exc)
                raise

    # ── Pull Request ──────────────────────────────────────────────────────────

    def create_pull_request(
        self,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
        labels: Optional[list[str]] = None,
        draft: bool = False,
    ) -> str:
        """Open a PR and apply labels. Returns the PR HTML URL."""
        # Ensure labels exist in repo
        if labels:
            self._ensure_labels(labels)

        try:
            pr = self._repo.create_pull(
                title=title,
                body=body,
                head=branch,
                base=base,
                draft=draft,
            )
            log.info("Pull request created: #%d — %s", pr.number, pr.html_url)
        except GithubException as exc:
            # PR may already exist
            if "A pull request already exists" in str(exc):
                log.warning("PR already exists for branch '%s' — skipping creation.", branch)
                pulls = self._repo.get_pulls(head=branch, state="open")
                pr = next(iter(pulls), None)
                if pr is None:
                    raise RuntimeError(f"Could not find existing PR for branch {branch}") from exc
            else:
                raise

        # Apply labels
        if labels:
            pr.add_to_labels(*labels)

        return pr.html_url

    def create_issue_comment_on_commit(self, branch: str, body: str) -> None:
        """Create a commit comment on the HEAD of the branch."""
        try:
            commit = self._repo.get_branch(branch).commit
            commit.create_comment(body)
            log.info("Comment added to HEAD of branch '%s'", branch)
        except GithubException as exc:
            log.warning("Could not post commit comment: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ensure_labels(self, labels: list[str]) -> None:
        """Create repository labels if they don't already exist."""
        existing = {lbl.name for lbl in self._repo.get_labels()}
        label_meta = {
            "infra-drift":   ("e11d48", "Automated infrastructure drift remediation"),
            "terraform":     ("7B42BC", "Terraform IaC changes"),
            "automated-pr":  ("0075ca", "Created by automation"),
        }
        for name in labels:
            if name not in existing:
                meta = label_meta.get(name, ("ededed", name))
                try:
                    self._repo.create_label(name=name, color=meta[0], description=meta[1])
                    log.info("Label '%s' created.", name)
                except GithubException:
                    pass  # race condition — label created by another run
