"""重复 Claim 清理脚本的行为测试。"""

from __future__ import annotations

import gc
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hl_mem.ingest.embedder import pack_vector
from hl_mem.storage.claims import ClaimRepository
from hl_mem.storage.database import Database
from scripts.cleanup_duplicates_v3 import cleanup_duplicates


class CleanupDuplicatesV3Test(unittest.TestCase):
    """验证清理脚本的预览、安全备份与证据合并。"""

    def test_dry_run_preserves_claims_and_execution_backs_up_and_merges_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "memory.db"
            backup_path = Path(directory) / "memory.db.backup"
            database = Database(db_path)
            connection = database.open()
            repo = ClaimRepository(connection)
            base = {
                "namespace_key": "default",
                "subject_entity_id": "workflow",
                "predicate": "事实",
                "recorded_from": "2026-01-01T00:00:00+00:00",
                "status": "active",
                "embedding_dense": pack_vector([1.0, 0.0]),
            }
            repo.insert_claim({**base, "id": "short", "value": "不要运行 pytest"})
            repo.insert_claim(
                {**base, "id": "long", "value": "工作流中不要运行 pytest"}
            )
            repo.insert_claim(
                {
                    **base,
                    "id": "other-namespace",
                    "namespace_key": "other",
                    "value": "不要运行 pytest",
                }
            )
            connection.execute(
                "INSERT INTO evidence_links("
                "id,derived_type,derived_id,evidence_type,evidence_id,relation"
                ") VALUES (?,?,?,?,?,?)",
                (
                    "evidence-short",
                    "claim",
                    "short",
                    "event",
                    "event-short",
                    "derived_from",
                ),
            )
            connection.execute(
                "INSERT INTO evidence_links("
                "id,derived_type,derived_id,evidence_type,evidence_id,relation"
                ") VALUES (?,?,?,?,?,?)",
                (
                    "evidence-long",
                    "claim",
                    "long",
                    "event",
                    "event-short",
                    "derived_from",
                ),
            )
            connection.commit()
            database.close()

            preview = cleanup_duplicates(db_path, backup_path, dry_run=True)
            self.assertEqual(preview["superseded"], 1)
            self.assertFalse(backup_path.exists())

            result = cleanup_duplicates(db_path, backup_path, dry_run=False)
            self.assertEqual(result["superseded"], 1)
            self.assertTrue(backup_path.exists())
            check = sqlite3.connect(db_path)
            self.assertEqual(
                check.execute(
                    "SELECT status FROM claims WHERE id=?", ("short",)
                ).fetchone()[0],
                "superseded",
            )
            self.assertEqual(
                check.execute(
                    "SELECT derived_id FROM evidence_links WHERE id=?",
                    ("evidence-long",),
                ).fetchone()[0],
                "long",
            )
            self.assertEqual(
                check.execute(
                    "SELECT status FROM claims WHERE id=?",
                    ("other-namespace",),
                ).fetchone()[0],
                "active",
            )
            self.assertEqual(
                check.execute(
                    "SELECT count(*) FROM evidence_links WHERE derived_id=? AND evidence_id=?",
                    ("long", "event-short"),
                ).fetchone()[0],
                1,
            )
            check.close()
            gc.collect()

    def test_existing_backup_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "memory.db"
            backup_path = Path(directory) / "memory.db.backup"
            database = Database(db_path)
            connection = database.open()
            repo = ClaimRepository(connection)
            base = {
                "namespace_key": "default",
                "subject_entity_id": "workflow",
                "predicate": "事实",
                "recorded_from": "2026-01-01T00:00:00+00:00",
                "status": "active",
                "embedding_dense": pack_vector([1.0, 0.0]),
            }
            repo.insert_claim({**base, "id": "short", "value": "不要运行 pytest"})
            repo.insert_claim(
                {**base, "id": "long", "value": "工作流中不要运行 pytest"}
            )
            database.close()
            backup_path.write_bytes(b"existing-backup")

            with self.assertRaises(FileExistsError):
                cleanup_duplicates(db_path, backup_path, dry_run=False)

            self.assertEqual(backup_path.read_bytes(), b"existing-backup")

    def test_concurrent_keeper_change_aborts_without_superseding_loser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "memory.db"
            backup_path = Path(directory) / "memory.db.backup"
            database = Database(db_path)
            connection = database.open()
            repo = ClaimRepository(connection)
            base = {
                "namespace_key": "default",
                "subject_entity_id": "workflow",
                "predicate": "事实",
                "recorded_from": "2026-01-01T00:00:00+00:00",
                "status": "active",
                "embedding_dense": pack_vector([1.0, 0.0]),
            }
            repo.insert_claim({**base, "id": "short", "value": "不要运行 pytest"})
            repo.insert_claim(
                {**base, "id": "long", "value": "工作流中不要运行 pytest"}
            )
            database.close()

            def mutate_after_backup(source: Path, target: Path) -> None:
                target.write_bytes(source.read_bytes())
                concurrent = sqlite3.connect(source)
                concurrent.execute(
                    "UPDATE claims SET value_json=? WHERE id=?",
                    ('"并发修改"', "long"),
                )
                concurrent.commit()
                concurrent.close()

            with patch(
                "scripts.cleanup_duplicates_v3._backup_database",
                side_effect=mutate_after_backup,
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    cleanup_duplicates(db_path, backup_path, dry_run=False)

            check = sqlite3.connect(db_path)
            self.assertEqual(
                check.execute(
                    "SELECT status FROM claims WHERE id=?", ("short",)
                ).fetchone()[0],
                "active",
            )
            check.close()


if __name__ == "__main__":
    unittest.main()
