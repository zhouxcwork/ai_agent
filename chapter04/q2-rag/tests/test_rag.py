import tempfile
import unittest
from pathlib import Path

from llama_index.core.embeddings import MockEmbedding
from llama_index.core.schema import NodeWithScore, TextNode

from ingest import build
from query import load_bundle, validate_citations
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


if __name__ == "__main__":
    unittest.main()
