import tempfile
import unittest
from pathlib import Path

from llama_index.core.embeddings import MockEmbedding
from llama_index.core.schema import NodeWithScore, TextNode

from ingest import build
from query import QUESTION, ground_answer, load_bundle, validate_citations
from rag.hybrid import BM25Retriever, rrf_fuse
from update_corpus import update
from verify_update import verify

CORPUS = Path(__file__).resolve().parents[1] / "corpus"


class RagTests(unittest.TestCase):
    def test_both_routes_reload_and_retire_old_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            embed = MockEmbedding(embed_dim=8)
            old = build(CORPUS, temp / "old", embed)
            update(CORPUS, temp / "corpus")
            new = build(temp / "corpus", temp / "new", embed)
            self.assertNotEqual(old["version"], new["version"])
            self.assertEqual(len(load_bundle(temp / "new", embed)[1]), len(new["node_ids"]))
            verify(temp / "old", temp / "new", embed)
            with self.assertRaises(ValueError):
                verify(temp / "old", temp / "old", embed)

    def test_rrf_prefers_joint_hits_and_bm25_excludes_zero(self):
        a, b, c = [TextNode(id_=key, text=key) for key in ("a", "b", "c")]
        rank = lambda *nodes: [NodeWithScore(node=node) for node in nodes]
        hits = rrf_fuse([rank(a, b), rank(c, b)])
        self.assertEqual([hit.node.node_id for hit in hits], ["b", "a", "c"])
        self.assertEqual(BM25Retriever([a, b, c]).retrieve("missing"), [])

    def test_citation_ids(self):
        self.assertEqual(validate_citations("30 分钟 [2][1][2]", 2), [2, 1])
        for answer in ("", "无引用", "[0]", "[3]", "[unknown]"):
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                validate_citations(answer, 2)

    def test_login_ttl_uses_cited_login_evidence(self):
        def hit(text):
            return NodeWithScore(node=TextNode(text=text))

        rule = hit("管理后台的登录会话有效期为 20 分钟。")
        comparison = hit("登录会话 20 分钟、可续期；支付会话 5 分钟。")
        payment = hit("支付会话的有效期为 5 分钟。")
        stale = hit("登录会话有效期为 30 分钟。")
        for question, answer in (
            (QUESTION, "登录会话有效期为 99 分钟。[1]"),
            (QUESTION, "支付会话有效期为 20 分钟。[1]"),
            (QUESTION, "登录会话有效期不是 20 分钟。[1]"),
            ("登录会话有效期是多少分钟？", "错误结论。[1][2]"),
            ("登录会话的有效期几分钟?", "错误结论。[1]"),
        ):
            with self.subTest(question=question, answer=answer):
                self.assertEqual(
                    ground_answer(question, answer, [rule, payment]),
                    ("登录会话的有效期为 20 分钟。[1]", [1]),
                )
        self.assertEqual(ground_answer(QUESTION, "5 分钟。[1]", [comparison])[1], [1])
        for answer, hits in (("5 分钟。[2]", [rule, payment]), ("20 分钟。[1][2]", [rule, stale])):
            with self.assertRaisesRegex(ValueError, "证据不支持"):
                ground_answer(QUESTION, answer, hits)
        self.assertEqual(
            ground_answer("支付会话的有效期是多少分钟？", "5 分钟。[1]", [payment]),
            ("5 分钟。[1]", [1]),
        )


if __name__ == "__main__":
    unittest.main()
