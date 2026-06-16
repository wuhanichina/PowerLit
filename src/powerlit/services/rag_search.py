from __future__ import annotations

import logging
from dataclasses import dataclass

from powerlit.services.rag_index import ChunkMetadata, RAGIndexService
from powerlit.settings import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchResult:
    doi: str
    title: str
    text: str
    score: float
    chunk_index: int


class RAGSearchService:
    """Service for searching the local vector index."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.indexer = RAGIndexService(settings)
        self._initialized = False

    def _ensure_loaded(self):
        if not self._initialized:
            if not self.indexer.load_index():
                # Attempt to build if missing? No, user should build explicitly or via watcher.
                logger.warning("RAG Index not found. Please build the index first.")
                return False
            self._initialized = True
        return True

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """Perform semantic search for the given query."""
        if not self._ensure_loaded():
            return []

        if not self.indexer._index or not self.indexer._metadata:
            return []

        # Generate query embedding
        query_embedding = self.indexer.model.encode([query], convert_to_numpy=True)
        self.indexer.faiss.normalize_L2(query_embedding)

        # Search in FAISS
        distances, indices = self.indexer._index.search(query_embedding, top_k)

        results = []
        for score, idx in zip(distances[0], indices[0], strict=False):
            if idx == -1 or idx >= len(self.indexer._metadata):
                continue

            meta: ChunkMetadata = self.indexer._metadata[idx]
            results.append(
                SearchResult(
                    doi=meta.doi,
                    title=meta.title,
                    text=meta.text_content,
                    score=float(score),
                    chunk_index=meta.chunk_index,
                )
            )

        return results
