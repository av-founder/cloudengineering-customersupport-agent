"""RETRIEVE (FR-06 / FR-09, DEP-04): manifest-locked official KB, heading-aware chunks, dense + BM25 RRF fusion, cross-encoder rerank."""
import asyncio
import difflib
import json
import logging
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import chromadb
import numpy as np
from rank_bm25 import BM25Okapi

from src.config import (BM25_K, CHUNK_MAX_TOKENS, CHUNK_OVERLAP, CITATION, DENSE_K, DOC_LINK, HEADER_PREFIX, IDK, KB_ARTICLE_COUNT,
                        KB_COLLECTION, KB_ORIGIN, KB_SOURCE_FILE, RERANK_CANDIDATES, RERANK_KEEP, RRF_K, TITLE_BOOST, TITLE_MAX_TOKENS, Settings,
                        load_prompt, sha256, terms)
from src.llm import LLMClient, LLMResult, LLMUnavailableError, usage_update

log = logging.getLogger(__name__)
_HEAD = re.compile(r"(?m)^(#{1,3})\s+(.+?)\s*$")


class KBContaminationError(RuntimeError):
    """FR-09_UNOFFICIAL_DATA_CONTAMINATION: something other than the manifest-locked official articles reached the KB path."""


# Input:  kb_path (Path) – the official documentation.json
# What:   Fingerprints the file and every article so any later change or contamination is detectable (kb_manifest.lock)
# Output: dict – manifest content
def build_manifest(kb_path: Path) -> dict[str, Any]:
    raw = kb_path.read_bytes()
    return {"allowed_source_file": KB_SOURCE_FILE, "article_count": len(docs := json.loads(raw)), "file_sha256": sha256(raw),
            "articles": {d["doc_id"]: sha256(d["content"]) for d in docs}}


# Input:  kb_path (Path); manifest_path (Path) – kb_manifest.lock
# What:   Refuses (fatal FR-09 error) anything that is not the exact, manifest-locked set of 29 official articles
#         (historical tickets, agent snippet files or edited docs all fail here, before any indexing happens)
# Output: list[dict] – verified official articles
def load_official_docs(kb_path: Path, manifest_path: Path) -> list[dict[str, Any]]:
    def fail(why: str) -> KBContaminationError:
        return KBContaminationError(f"FR-09_UNOFFICIAL_DATA_CONTAMINATION: {why}")
    if kb_path.name != KB_SOURCE_FILE:
        raise fail(f"'{kb_path.name}' is not the approved knowledge source '{KB_SOURCE_FILE}'")
    if not manifest_path.is_file():
        raise fail(f"manifest '{manifest_path.name}' is missing")
    lock, raw = json.loads(manifest_path.read_text(encoding="utf-8")), kb_path.read_bytes()
    if sha256(raw) != lock.get("file_sha256"):
        raise fail(f"'{kb_path.name}' does not match the SHA-256 recorded in {manifest_path.name}")
    docs = json.loads(raw)
    bad = [d for d in docs if not isinstance(d, dict) or {"doc_id", "title", "content"} - d.keys() or {"body", "labels", "ticket_id"} & d.keys()]
    if bad or len(docs) != KB_ARTICLE_COUNT or {d["doc_id"]: sha256(d["content"]) for d in docs} != lock.get("articles"):
        raise fail(f"record set is not the {KB_ARTICLE_COUNT} manifest-locked official articles")
    return docs


# Input:  s (str)
# What:   Approximate token count (words + punctuation), close to word-piece counts for this English corpus
# Output: int
def _ntok(s: str) -> int:
    return len(re.findall(r"\w+|[^\w\s]", s))


# Input:  text (str) – one section body; max_t (int); overlap (int)
# What:   Keeps short sections whole; packs long ones paragraph by paragraph (never splitting ``` code fences) into windows of
#         <= max_t tokens, carrying the last ~overlap words into the next window
# Output: list[str] – one or more chunk bodies
def _windows(text: str, max_t: int = CHUNK_MAX_TOKENS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if _ntok(text) <= max_t:
        return [text]
    paras, buf, fence = [], [], False
    for ln in text.splitlines():
        fence ^= ln.lstrip().startswith("```")
        if ln.strip() or fence:
            buf.append(ln)
        elif buf:
            paras, buf = paras + ["\n".join(buf)], []
    paras += ["\n".join(buf)] if buf else []
    out, cur = [], []
    for p in paras:
        if cur and _ntok("\n\n".join(cur + [p])) > max_t:
            out.append("\n\n".join(cur))
            cur = [" ".join(" ".join(cur).split()[-overlap:])]
        cur.append(p)
    return out + ["\n\n".join(cur)]


# Input:  doc (dict) – one official article
# What:   Splits the Markdown at #/##/### headings and stamps every chunk with its [Doc ID | Article | Section] anchor so the
#         chunk always points back to its parent article
# Output: list[dict] – chunks with id, metadata, plain text and the prefixed text that gets indexed
def chunk_article(doc: dict[str, Any]) -> list[dict[str, Any]]:
    c, marks = doc["content"], list(_HEAD.finditer(doc["content"]))
    spans = [("Overview" if m[1] == "#" else m[2], m.end(), marks[i + 1].start() if i + 1 < len(marks) else len(c)) for i, m in enumerate(marks)]
    if not marks or c[: marks[0].start()].strip():
        spans.insert(0, ("Overview", 0, marks[0].start() if marks else len(c)))
    out: list[dict[str, Any]] = []
    for head, a, b in spans:
        for w in filter(None, _windows(c[a:b].strip())):
            meta = {"doc_id": doc["doc_id"], "article_title": doc["title"], "section_heading": head}
            out.append(meta | {"chunk_id": f"{doc['doc_id']}::{len(out):02d}", "doc_link": DOC_LINK.format(doc_id=doc["doc_id"]),
                               "text": w, "chunk_text": HEADER_PREFIX.format(**meta) + w})
    return out


# Input:  doc (dict) – one official article
# What:   Sentence-level evidence for the NLI check (the model is reliable on short premises, not on whole sections): every
#         sentence or numbered step, bullet items prefixed with their section ("Common causes: ..."), the "applies to" line,
#         plus each pair of neighbouring sentences in the same section for claims that combine two facts
# Output: list[str]
def evidence_units(doc: dict[str, Any]) -> list[str]:
    units: list[str] = []
    for ch in chunk_article(doc):
        sents: list[str] = []
        for ln in ch["text"].splitlines():
            if m := re.match(r"^\s*\*\*Applies to:\*\*\s*(.+)", ln):
                sents.append(f"{doc['title']} applies to {m[1].strip()}.")
            elif m := re.match(r"^\s*[-*]\s+(.+)", ln):
                sents.append(f"{ch['section_heading']}: {m[1].strip()}")
            elif body := re.sub(r"^\s*\d+[.)]\s+", "", ln).strip():
                sents += [s for s in re.split(r"(?<=[.!?])\s+", body) if len(s.split()) >= 3]
        units += sents + [f"{a} {b}" for a, b in zip(sents, sents[1:])]
    return units


class KnowledgeBase:
    # Input:  docs (list[dict]) – verified official articles; embed (callable texts -> np.ndarray, L2-normalised);
    #         rerank (callable pairs -> np.ndarray of 0..1 relevance); chroma_path (Path | None) – None means in-memory;
    #         embed_id (str) – embedding model name, part of the index fingerprint
    # What:   Holds the official article registry and the hybrid index (ChromaDB dense vectors + BM25 lexical) and runs retrieval
    # Output: KnowledgeBase
    def __init__(self, docs: list[dict], *, embed: Callable, rerank: Callable, chroma_path: Path | None = None, embed_id: str = "custom"):
        self.registry = {d["doc_id"]: {"doc_id": d["doc_id"], "title": d["title"], "doc_link": DOC_LINK.format(doc_id=d["doc_id"]),
                                       "category": d.get("category") or "general", "content": d["content"], "content_sha256": sha256(d["content"])}
                         for d in docs}
        self.by_title = {r["title"].lower(): k for k, r in self.registry.items()}
        self.chunks = [ch for d in docs for ch in chunk_article(d)]
        self.evidence = {d["doc_id"]: evidence_units(d) for d in docs}
        self.row = {c["chunk_id"]: i for i, c in enumerate(self.chunks)}
        self.embed, self.rerank, self.excluded, self._lock = embed, rerank, set(), threading.Lock()
        self.bm25 = BM25Okapi([terms(c["chunk_text"]) for c in self.chunks])
        self.col = self._index(chroma_path, sha256(json.dumps([c["chunk_text"] for c in self.chunks]) + embed_id))

    def _index(self, path: Path | None, fingerprint: str) -> Any:
        # Input:  path (Path | None); fingerprint (str) – hash of chunk texts + embedding model
        # What:   Reuses the persisted ChromaDB collection when it matches the fingerprint, otherwise rebuilds it from the
        #         official chunks only (every vector is tagged source_origin = OFFICIAL_KB_ONLY). In-memory indexes share one
        #         process-wide store, so their collection name carries the fingerprint to keep instances isolated.
        # Output: chromadb Collection
        cfg = chromadb.config.Settings(anonymized_telemetry=False)
        client = chromadb.PersistentClient(path=str(path), settings=cfg) if path else chromadb.EphemeralClient(settings=cfg)
        name = KB_COLLECTION if path else f"{KB_COLLECTION}-{fingerprint[:12]}"
        try:
            col = client.get_collection(name, embedding_function=None)
            if (col.metadata or {}).get("kb_fingerprint") == fingerprint and col.count() == len(self.chunks):
                return col
            client.delete_collection(name)
        except Exception:  # collection missing or unreadable -> rebuild
            pass
        col = client.create_collection(name, embedding_function=None, configuration={"hnsw": {"space": "cosine"}},
                                       metadata={"kb_fingerprint": fingerprint, "source_file": KB_SOURCE_FILE})
        meta = lambda c: {k: c[k] for k in ("doc_id", "article_title", "section_heading", "doc_link")} | {
            "source_file": KB_SOURCE_FILE, "source_origin": KB_ORIGIN, "content_sha256": self.registry[c["doc_id"]]["content_sha256"]}
        col.add(ids=[c["chunk_id"] for c in self.chunks], embeddings=np.asarray(self.embed([c["chunk_text"] for c in self.chunks]), dtype=np.float32),
                documents=[c["chunk_text"] for c in self.chunks], metadatas=[meta(c) for c in self.chunks])
        return col

    def search(self, query: str, matched_title: str | None = None, max_candidates: int | None = RERANK_CANDIDATES) -> list[dict[str, Any]]:
        # Input:  query (str) – sanitised ticket text (+ FR-06 title); matched_title (str | None) – exact official title or None;
        #         max_candidates (int | None) – how many fused candidates the cross-encoder scores (matched-article sections always kept)
        # What:   Dense top-10 (ChromaDB, official-only filter) + BM25 top-10 -> Reciprocal Rank Fusion (+ title boost, which also
        #         pulls the matched article into the pool) -> cross-encoder relevance (0..1) for the best candidates, best first
        # Output: list[dict] – candidate passages with dense / bm25 / rrf / rerank scores
        with self._lock:
            where: dict[str, Any] = {"source_origin": KB_ORIGIN}
            if self.excluded:
                where = {"$and": [where, {"doc_id": {"$nin": sorted(self.excluded)}}]}
            res = self.col.query(query_embeddings=np.asarray(self.embed([query]), dtype=np.float32), n_results=DENSE_K, where=where, include=["distances"])
            dense = {cid: 1.0 - d for cid, d in zip(res["ids"][0], res["distances"][0])}
            bm = self.bm25.get_scores(terms(query))
            ok = [i for i, c in enumerate(self.chunks) if c["doc_id"] not in self.excluded]
            rrf: dict[int, float] = defaultdict(float)
            for r, cid in enumerate(dense):
                rrf[self.row[cid]] += 1 / (RRF_K + r + 1)
            for r, i in enumerate(i for i in sorted(ok, key=lambda i: -bm[i])[:BM25_K] if bm[i] > 0):
                rrf[i] += 1 / (RRF_K + r + 1)
            if (tdoc := self.by_title.get((matched_title or "").lower())) and tdoc not in self.excluded:
                for i in ok:
                    rrf[i] += TITLE_BOOST if self.chunks[i]["doc_id"] == tdoc else 0.0
            cand = [i for i in sorted(rrf, key=lambda i: -rrf[i]) if rrf[i] > 0]
            if max_candidates:
                cand = list(dict.fromkeys(cand[:max_candidates] + [i for i in cand if self.chunks[i]["doc_id"] == tdoc]))
            scores = np.asarray(self.rerank([(query, self.chunks[i]["chunk_text"]) for i in cand]), dtype=float) if cand else []
        keys = ("doc_id", "chunk_id", "article_title", "section_heading", "doc_link", "chunk_text")
        out = [{k: self.chunks[i][k] for k in keys} | {"dense_score": round(float(dense.get(self.chunks[i]["chunk_id"], 0.0)), 4),
                                                         "bm25_score": round(float(bm[i]), 4), "rrf_score": round(rrf[i], 5),
                                                         "rerank_score": round(float(s), 4)} for i, s in zip(cand, scores)]
        return sorted(out, key=lambda c: -c["rerank_score"])

    def premises(self, doc_ids: list[str]) -> list[str]:
        # Input:  doc_ids (list[str]) – cited articles
        # What:   Sentence-level NLI evidence from the cited official articles only
        # Output: list[str]
        return [u for d in dict.fromkeys(doc_ids) if d in self.registry for u in self.evidence[d]]

    def citation(self, doc_id: str) -> str:
        # Input:  doc_id (str) – official article id
        # What:   The exact FR-03 reference footer for that article
        # Output: str
        r = self.registry[doc_id]
        return CITATION.format(doc_id=doc_id, article_title=r["title"], doc_link=r["doc_link"])

    @classmethod
    def from_settings(cls, s: Settings, *, persist: bool = True) -> "KnowledgeBase":
        # Input:  s (Settings); persist (bool) – keep the ChromaDB index on disk
        # What:   Verifies the FR-09 manifest, loads the local MiniLM embedder and MS-MARCO cross-encoder, then builds the index
        # Output: KnowledgeBase
        import torch
        from sentence_transformers import CrossEncoder, SentenceTransformer
        docs = load_official_docs(s.kb_path, s.kb_manifest_path)
        emb, ce, sig = SentenceTransformer(s.embedding_model, device="cpu"), CrossEncoder(s.reranker_model, device="cpu"), torch.nn.Sigmoid()
        embed = lambda texts: emb.encode(list(texts), normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True)
        rerank = lambda pairs: ce.predict(pairs, activation_fn=sig, batch_size=32, show_progress_bar=False)
        return cls(docs, embed=embed, rerank=rerank, chroma_path=s.chroma_path if persist else None, embed_id=s.embedding_model)


# Input:  cands (list[dict]) – reranked candidates, best first; n (int)
# What:   Best-scoring passage per article, used as "Attempted Docs" for the human hand-off (FR-07)
# Output: list[dict] – doc_id, article_title, doc_link, rerank_score
def attempted_sources(cands: list[dict[str, Any]], n: int = 5) -> list[dict[str, Any]]:
    best: dict[str, dict] = {}
    for c in cands:
        best.setdefault(c["doc_id"], c)
    return [{k: c[k] for k in ("doc_id", "article_title", "doc_link", "rerank_score")} for c in list(best.values())[:n]]


# Input:  st (TicketState); llm (LLMClient); kb (KnowledgeBase)
# What:   FR-06 Semantic Search Assistant: maps the customer's own words (synonyms, broken English) to one exact official title
# Output: tuple[str, LLMResult] – exact title or "I Don't Know", plus call metadata
async def match_title(st: Any, *, llm: LLMClient, kb: KnowledgeBase) -> tuple[str, LLMResult]:
    meta, tpl = load_prompt("FR-06_semantic_title_matcher")
    titles = "\n".join(f"- {r['title']}" for d, r in kb.registry.items() if d not in kb.excluded)
    msgs = [{"role": "system", "content": tpl.safe_substitute(titles=titles)}, {"role": "user", "content": st.ticket_block()}]
    res = await llm.complete_json(msgs, temperature=float(meta.get("temperature", 0)), max_tokens=TITLE_MAX_TOKENS)
    t = str(res.data.get("matched_title", "")).strip().strip('"').lower()
    key = t if t in kb.by_title else next(iter(difflib.get_close_matches(t, kb.by_title, n=1, cutoff=0.9)), None)
    ok = key and kb.by_title[key] not in kb.excluded
    return (kb.registry[kb.by_title[key]]["title"] if ok else IDK), res


# Input:  st (TicketState); kb (KnowledgeBase); llm (LLMClient); tau_ret (float) – relevance cut-off from the calibration file
# What:   Title bridging (degrades to "I Don't Know" if the LLM is down) -> hybrid search -> keeps at most 3 passages scoring
#         >= tau_ret ("returns nothing rather than something irrelevant"), records the best rejected score and attempted docs
# Output: dict – TicketState updates
async def retrieve_ticket(st: Any, *, kb: KnowledgeBase, llm: LLMClient, tau_ret: float) -> dict[str, Any]:
    try:
        title, res = await match_title(st, llm=llm, kb=kb)
    except LLMUnavailableError:
        title, res = IDK, None
    text = st.ticket_text()
    cands = await asyncio.to_thread(kb.search, text if title == IDK else f"{text}\n{title}", None if title == IDK else title)
    kept = [c for c in cands if c["rerank_score"] >= tau_ret][:RERANK_KEEP]
    return usage_update(st, [res] if res else []) | {
        "semantic_matched_title": title, "retrieval_executed": True, "retrieved_passages": kept, "attempted_sources": attempted_sources(cands),
        "highest_rejected_retrieval_score": max((c["rerank_score"] for c in cands if c["rerank_score"] < tau_ret), default=0.0),
        "top_retrieval_score": cands[0]["rerank_score"] if cands else 0.0,
    }
