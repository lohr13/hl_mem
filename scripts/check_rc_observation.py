#!/usr/bin/env python
"""Validate the immutable seven-day Core 1.0 release-candidate observation window."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

RC_TAG_PATTERN = re.compile(r"^v1\.0\.0rc[1-9][0-9]*$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_GATES = ("quality_smoke", "public_recall", "migration", "security")
GITHUB_API = "https://api.github.com"


def _datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} is missing")
    return date.fromisoformat(value)


def _labels(issue: Mapping[str, Any]) -> set[str]:
    labels: set[str] = set()
    for item in issue.get("labels", []):
        if isinstance(item, str):
            labels.add(item)
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            labels.add(str(item["name"]))
    return labels


def _longest_consecutive(dates: set[date]) -> int:
    longest = current = 0
    previous: date | None = None
    for value in sorted(dates):
        current = current + 1 if previous is not None and value == previous + timedelta(days=1) else 1
        longest = max(longest, current)
        previous = value
    return longest


def evaluate(
    release: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    now: datetime,
) -> list[str]:
    """Return every promotion-policy violation without performing network access."""
    failures: list[str] = []
    now = now.astimezone(timezone.utc)
    tag = str(release.get("tag", ""))
    commit = str(release.get("commit", ""))
    if not RC_TAG_PATTERN.fullmatch(tag):
        failures.append(f"release tag is not a Core 1.0 RC: {tag!r}")
    if not COMMIT_PATTERN.fullmatch(commit):
        failures.append("release commit is not a lowercase 40-hex object ID")
    if bool(release.get("draft")):
        failures.append("release is still a draft")
    if release.get("prerelease") is not True:
        failures.append("release is not marked as a prerelease")
    try:
        published_at = _datetime(release.get("published_at"), "release published_at")
    except (TypeError, ValueError) as error:
        failures.append(str(error))
        published_at = now
    age_hours = (now - published_at).total_seconds() / 3600.0
    if age_hours < 168.0:
        failures.append(f"release observation age is {age_hours:.1f} hours; at least 168 hours are required")

    valid_dates: set[date] = set()
    for index, artifact in enumerate(artifacts, start=1):
        label = str(artifact.get("name") or f"artifact {index}")
        artifact_valid = True
        artifact_tag = str(artifact.get("tag", ""))
        artifact_commit = str(artifact.get("commit", ""))
        if artifact_tag != tag:
            failures.append(f"{label}: tag {artifact_tag!r} does not match release tag {tag!r}")
            artifact_valid = False
        if artifact_commit != commit:
            failures.append(f"{label}: commit does not match immutable release commit")
            artifact_valid = False
        if artifact.get("workflow_head_sha") not in (None, commit):
            failures.append(f"{label}: workflow head SHA does not match immutable release commit")
            artifact_valid = False
        if artifact.get("workflow_conclusion") != "success":
            failures.append(f"{label}: workflow run did not conclude successfully")
            artifact_valid = False
        if artifact.get("expired") is True:
            failures.append(f"{label}: artifact has expired")
            artifact_valid = False
        for gate in REQUIRED_GATES:
            if artifact.get(gate) != "passed":
                failures.append(f"{label}: {gate} is not passed")
                artifact_valid = False
        run_url = artifact.get("run_url")
        if (
            not isinstance(run_url, str)
            or not run_url.startswith("https://github.com/")
            or "/actions/runs/" not in run_url
        ):
            failures.append(f"{label}: run_url is not a GitHub Actions run")
            artifact_valid = False
        try:
            utc_date = _date(artifact.get("utc_date"), f"{label} utc_date")
        except (TypeError, ValueError) as error:
            failures.append(str(error))
            artifact_valid = False
            continue
        expected_name = f"rc-observation-{tag}-{utc_date.isoformat()}"
        if artifact.get("name") != expected_name:
            failures.append(f"{label}: artifact name must be {expected_name!r}")
            artifact_valid = False
        if utc_date < published_at.date() or utc_date > now.date():
            failures.append(f"{label}: UTC date lies outside the release observation interval")
            artifact_valid = False
        if artifact_valid:
            valid_dates.add(utc_date)

    longest_run = _longest_consecutive(valid_dates)
    if longest_run < 7:
        failures.append(f"seven consecutive UTC dates are required; longest valid run is {longest_run}")

    for issue in issues:
        if issue.get("state") != "open" or "pull_request" in issue:
            continue
        if not _labels(issue).intersection({"priority:P0", "priority:P1"}):
            continue
        try:
            created_at = _datetime(issue.get("created_at"), "issue created_at")
        except (TypeError, ValueError) as error:
            failures.append(str(error))
            continue
        if created_at >= published_at:
            failures.append(f"open P0/P1 issue #{issue.get('number', '?')} was created during RC observation")
    return failures


def _request(url: str, token: str, *, raw: bool = False) -> tuple[Any, Mapping[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "hl-mem-rc-observation",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
        if raw:
            return payload, dict(response.headers)
        return json.loads(payload.decode("utf-8")), dict(response.headers)


def _pages(url: str, key: str | None, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, 11):
        separator = "&" if "?" in url else "?"
        payload, _headers = _request(f"{url}{separator}per_page=100&page={page}", token)
        if key is None and isinstance(payload, list):
            page_items = payload
        else:
            page_items = payload.get(key, []) if isinstance(payload, Mapping) and key is not None else []
        if not isinstance(page_items, list):
            raise ValueError(f"GitHub response field {key!r} is not a list")
        items.extend(item for item in page_items if isinstance(item, dict))
        if len(page_items) < 100:
            return items
    raise ValueError(f"GitHub pagination exceeded 1000 {key}")


def _tag_commit(repository: str, tag: str, token: str) -> str:
    encoded_tag = urllib.parse.quote(tag, safe="")
    payload, _headers = _request(f"{GITHUB_API}/repos/{repository}/git/ref/tags/{encoded_tag}", token)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("object"), Mapping):
        raise ValueError("GitHub tag reference response is malformed")
    target = payload["object"]
    for _depth in range(5):
        target_type, sha = target.get("type"), str(target.get("sha", ""))
        if target_type == "commit" and COMMIT_PATTERN.fullmatch(sha):
            return sha
        if target_type != "tag" or not COMMIT_PATTERN.fullmatch(sha):
            break
        tag_payload, _headers = _request(f"{GITHUB_API}/repos/{repository}/git/tags/{sha}", token)
        if not isinstance(tag_payload, Mapping) or not isinstance(tag_payload.get("object"), Mapping):
            break
        target = tag_payload["object"]
    raise ValueError("release tag does not resolve to an immutable commit")


def _artifact_payload(repository: str, artifact: Mapping[str, Any], token: str) -> dict[str, Any]:
    artifact_id = artifact.get("id")
    archive, _headers = _request(
        f"{GITHUB_API}/repos/{repository}/actions/artifacts/{artifact_id}/zip",
        token,
        raw=True,
    )
    if not isinstance(archive, bytes):
        raise ValueError(f"artifact {artifact_id} download did not return a zip archive")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        json_members = [name for name in bundle.namelist() if name.endswith(".json") and not name.endswith("/")]
        if len(json_members) != 1:
            raise ValueError(f"artifact {artifact_id} must contain exactly one JSON payload")
        payload = json.loads(bundle.read(json_members[0]).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"artifact {artifact_id} JSON payload is not an object")
    return payload


def _remote_inputs(
    repository: str, tag: str, token: str
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    encoded_tag = urllib.parse.quote(tag, safe="")
    raw_release, _headers = _request(f"{GITHUB_API}/repos/{repository}/releases/tags/{encoded_tag}", token)
    if not isinstance(raw_release, Mapping) or raw_release.get("tag_name") != tag:
        raise ValueError("GitHub release response does not match the requested tag")
    commit = _tag_commit(repository, tag, token)
    release = {
        "tag": tag,
        "commit": commit,
        "published_at": raw_release.get("published_at"),
        "draft": raw_release.get("draft"),
        "prerelease": raw_release.get("prerelease"),
    }
    prefix = f"rc-observation-{tag}-"
    raw_artifacts = _pages(f"{GITHUB_API}/repos/{repository}/actions/artifacts", "artifacts", token)
    artifacts: list[dict[str, Any]] = []
    for artifact in raw_artifacts:
        if not str(artifact.get("name", "")).startswith(prefix):
            continue
        run = artifact.get("workflow_run")
        if not isinstance(run, Mapping) or not run.get("id"):
            raise ValueError(f"artifact {artifact.get('id')} has no workflow run")
        run_payload, _headers = _request(f"{GITHUB_API}/repos/{repository}/actions/runs/{run['id']}", token)
        if not isinstance(run_payload, Mapping):
            raise ValueError(f"workflow run {run['id']} response is malformed")
        payload = _artifact_payload(repository, artifact, token)
        payload.update(
            name=artifact.get("name"),
            expired=artifact.get("expired"),
            workflow_conclusion=run_payload.get("conclusion"),
            workflow_head_sha=run_payload.get("head_sha"),
        )
        artifacts.append(payload)
    since = urllib.parse.quote(str(raw_release.get("published_at", "")), safe="")
    issues = _pages(f"{GITHUB_API}/repos/{repository}/issues?state=open&since={since}", None, token)
    return release, artifacts, issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub OWNER/REPOSITORY")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--token-env", required=True, help="environment variable containing a GitHub token")
    arguments = parser.parse_args(argv)
    token = os.environ.get(arguments.token_env)
    if not token:
        print(f"RC observation failed: token environment variable {arguments.token_env!r} is empty")
        return 1
    try:
        release, artifacts, issues = _remote_inputs(arguments.repository, arguments.tag, token)
        failures = evaluate(release, artifacts, issues, datetime.now(timezone.utc))
    except (OSError, TypeError, ValueError, zipfile.BadZipFile, json.JSONDecodeError, urllib.error.URLError) as error:
        print(f"RC observation failed: {error}")
        return 1
    if failures:
        print("RC observation failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"RC observation passed for {arguments.tag}: seven consecutive UTC dates and no open P0/P1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
