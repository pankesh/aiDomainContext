import uuid

from aidomaincontext.retrieval.hybrid_search import reciprocal_rank_fusion


def _make_chunk(chunk_id=None, score=0.5):
    return {
        "id": chunk_id or uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "chunk_index": 0,
        "content": "test content",
        "token_count": 5,
        "score": score,
    }


def test_rrf_single_list():
    c1 = _make_chunk()
    c2 = _make_chunk()
    results = reciprocal_rank_fusion([[c1, c2]])
    assert len(results) == 2
    assert results[0]["score"] > results[1]["score"]


def test_rrf_merges_duplicates():
    shared_id = uuid.uuid4()
    c1 = _make_chunk(chunk_id=shared_id)
    c2 = _make_chunk(chunk_id=shared_id)
    c3 = _make_chunk()

    results = reciprocal_rank_fusion([[c1, c3], [c2]])
    # shared_id appears in both lists, so should rank higher
    assert results[0]["id"] == shared_id


def test_rrf_empty():
    results = reciprocal_rank_fusion([[], []])
    assert results == []


def test_rrf_k_parameter():
    c1 = _make_chunk()
    c2 = _make_chunk()
    results_k10 = reciprocal_rank_fusion([[c1, c2]], k=10)
    results_k100 = reciprocal_rank_fusion([[c1, c2]], k=100)
    # With higher k, scores are more compressed
    diff_k10 = results_k10[0]["score"] - results_k10[1]["score"]
    diff_k100 = results_k100[0]["score"] - results_k100[1]["score"]
    assert diff_k10 > diff_k100
