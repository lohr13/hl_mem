"""文档一致性检查的相对链接回归测试。"""

from pathlib import Path

import pytest

from scripts import check_docs_consistency as checker
from scripts.generate_configuration_reference import ROOT, generate


def test_configuration_reference_matches_generator() -> None:
    assert (ROOT / "docs/configuration.md").read_text(encoding="utf-8") == generate()


def test_configuration_reference_documents_non_negative_request_limit() -> None:
    request_limit_row = next(line for line in generate().splitlines() if "`server.max_request_body`" in line)

    assert "| >= 0 |" in request_limit_row


def test_find_broken_relative_links_reports_only_missing_local_target(tmp_path: Path) -> None:
    """缺失的仓库内链接必须失败，外部链接和页内锚点不得误报。"""

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "target.md").write_text("# Target\n", encoding="utf-8")
    source = docs / "guide.md"
    source.write_text(
        "\n".join(
            (
                "[exists](target.md)",
                "[missing](missing.md#section)",
                "[external](https://example.com/docs)",
                "[anchor](#local-section)",
            )
        ),
        encoding="utf-8",
    )

    find_broken = getattr(checker, "find_broken_relative_links", lambda *_args: [])

    assert find_broken(tmp_path, [source]) == ["docs/guide.md -> missing.md#section"]


def test_latest_changelog_entry_accepts_release_candidate() -> None:
    version, body = checker.latest_changelog_entry("## v1.0.0rc1\n\n- candidate\n\n## v0.36.1\n\n- old\n")

    assert version == "1.0.0rc1"
    assert "candidate" in body
    assert "old" not in body


def test_version_extractors_preserve_release_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "src/hl_mem").mkdir(parents=True)
    (tmp_path / "src/hl_mem/__init__.py").write_text('__version__ = "1.0.0rc1"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0rc1"\n', encoding="utf-8")
    monkeypatch.setattr(checker, "ROOT", tmp_path)

    assert checker.get_version() == "1.0.0rc1"
    assert checker.get_project_version() == "1.0.0rc1"


def test_latest_changelog_entry_keeps_stable_version() -> None:
    assert checker.latest_changelog_entry("## 1.0.0\n\n- stable\n") == ("1.0.0", "\n\n- stable\n")
