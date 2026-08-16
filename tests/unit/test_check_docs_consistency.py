"""文档一致性检查的相对链接回归测试。"""

from pathlib import Path

from scripts import check_docs_consistency as checker
from scripts.generate_configuration_reference import ROOT, generate


def test_configuration_reference_matches_generator() -> None:
    assert (ROOT / "docs/configuration.md").read_text(encoding="utf-8") == generate()


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
