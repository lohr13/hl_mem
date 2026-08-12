from __future__ import annotations

import unittest

import numpy as np

from evaluation.tools import run_longmemeval_benchmark as runner
from evaluation.tools.longmemeval import native_rag


def _case() -> runner.LongMemEvalCase:
    return runner.LongMemEvalCase(
        case_id="native-rag-case",
        question_type="multi-session",
        question="Where did I study?",
        answer="Paris",
        question_at="2024-01-04T12:00:00+00:00",
        sessions=(
            runner.SessionInput(
                session_id="late",
                event_id="event-late",
                occurred_at="2024-01-03T09:00:00+00:00",
                messages=({"role": "assistant", "content": "later answer"},),
            ),
            runner.SessionInput(
                session_id="early",
                event_id="event-early",
                occurred_at="2024-01-01T09:00:00+00:00",
                messages=({"role": "user", "content": "I studied in Paris."},),
            ),
            runner.SessionInput(
                session_id="middle",
                event_id="event-middle",
                occurred_at="2024-01-02T09:00:00+00:00",
                messages=(
                    {"role": "user", "content": "middle question"},
                    {"role": "assistant", "content": "middle answer"},
                ),
            ),
        ),
        gold_event_ids=("event-early",),
        gold_session_ids=("early",),
    )


class RawSessionRetrievalTests(unittest.TestCase):
    def test_documents_preserve_complete_raw_roles_text_and_timestamp(self) -> None:
        documents = native_rag.render_raw_session_documents(_case())

        self.assertEqual([document.session_id for document in documents], ["late", "early", "middle"])
        self.assertIn("Session Date: 2024-01-01T09:00:00+00:00", documents[1].text)
        self.assertIn('"role":"user","content":"I studied in Paris."', documents[1].text)
        self.assertEqual(documents[2].message_count, 2)
        self.assertNotIn("has_answer", "".join(document.text for document in documents))

    def test_exact_cosine_selects_top_k_and_breaks_ties_by_source_order(self) -> None:
        documents = native_rag.render_raw_session_documents(_case())
        vectors = np.asarray(
            [
                [1.0, 0.0],
                [1.0, 0.0],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        )

        hits = native_rag.select_raw_sessions(
            documents,
            vectors,
            np.asarray([1.0, 0.0], dtype=np.float32),
            top_k=2,
        )

        self.assertEqual([hit.document.session_id for hit in hits], ["late", "early"])
        self.assertEqual([hit.retrieval_rank for hit in hits], [1, 2])
        self.assertEqual([hit.score for hit in hits], [1.0, 1.0])

    def test_reader_pack_preserves_selection_but_orders_hits_by_time(self) -> None:
        documents = native_rag.render_raw_session_documents(_case())
        vectors = np.asarray(
            [
                [1.0, 0.0],
                [0.8, 0.2],
                [0.9, 0.1],
            ],
            dtype=np.float32,
        )
        hits = native_rag.select_raw_sessions(
            documents,
            vectors,
            np.asarray([1.0, 0.0], dtype=np.float32),
            top_k=3,
        )

        rendered = native_rag.render_native_rag_user_prompt(_case(), hits)

        self.assertEqual(rendered.retrieval_session_ids, ("late", "middle", "early"))
        self.assertEqual(rendered.reader_session_ids, ("early", "middle", "late"))
        self.assertLess(rendered.prompt.index("I studied in Paris."), rendered.prompt.index("middle question"))
        self.assertLess(rendered.prompt.index("middle question"), rendered.prompt.index("later answer"))
        self.assertTrue(rendered.prompt.endswith("Question: Where did I study?\nAnswer:"))

    def test_selection_validates_top_k_and_vector_shapes(self) -> None:
        documents = native_rag.render_raw_session_documents(_case())
        vectors = np.ones((3, 2), dtype=np.float32)

        with self.assertRaisesRegex(ValueError, "top_k"):
            native_rag.select_raw_sessions(documents, vectors, np.ones(2, dtype=np.float32), top_k=0)
        with self.assertRaisesRegex(ValueError, "document vector count"):
            native_rag.select_raw_sessions(documents, vectors[:2], np.ones(2, dtype=np.float32), top_k=2)
        with self.assertRaisesRegex(ValueError, "dimensions"):
            native_rag.select_raw_sessions(documents, vectors, np.ones(3, dtype=np.float32), top_k=2)


if __name__ == "__main__":
    unittest.main()
