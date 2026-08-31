from __future__ import annotations

from pathlib import Path

from scripts import check_actions_pinned


def test_unpinned_remote_action_is_rejected(tmp_path: Path) -> None:
    workflow = tmp_path / "bad.yml"
    workflow.write_text("steps:\n  - uses: actions/checkout@v5\n", encoding="utf-8")

    assert check_actions_pinned.check_paths([workflow]) == [
        "bad.yml:2: remote action is not pinned to a full commit SHA"
    ]


def test_sha_pinned_and_local_actions_are_accepted(tmp_path: Path) -> None:
    workflow = tmp_path / "good.yml"
    workflow.write_text(
        "steps:\n"
        f"  - uses: actions/checkout@{'a' * 40} # v5\n"
        "  - uses: ./local\n"
        f"  - uses: docker://example/tool@sha256:{'b' * 64}\n",
        encoding="utf-8",
    )

    assert check_actions_pinned.check_paths([workflow]) == []


def test_docker_tag_without_digest_is_rejected(tmp_path: Path) -> None:
    workflow = tmp_path / "docker.yml"
    workflow.write_text("steps:\n  - uses: docker://example/tool:latest\n", encoding="utf-8")

    assert check_actions_pinned.check_paths([workflow]) == [
        "docker.yml:2: Docker action is not pinned to a sha256 digest"
    ]
