from __future__ import annotations

import json
import logging
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from powerlit.settings import Settings

logger = logging.getLogger(__name__)

VECTOR_DEPENDENCY_HINT = (
    "Semantic vector RAG requires faiss-cpu and sentence-transformers. "
    "They are not installed by default because PowerLit 0.2.0b1 keeps local "
    "evidence retrieval lightweight. Install them in your environment before "
    "using 'powerlit rag build-index/search'."
)


class RAGVectorDependencyError(RuntimeError):
    """Raised when the optional semantic vector stack is unavailable."""


@dataclass(slots=True)
class ChunkMetadata:
    doi: str
    title: str
    chunk_index: int
    text_content: str


class RAGIndexService:
    """Service for managing the local vector index using BGE-M3 and FAISS."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.index_dir = settings.vector_index_dir
        self.faiss_path = self.index_dir / "index.faiss"
        self.metadata_path = self.index_dir / "metadata.pkl"

        self._faiss: Any | None = None
        self._sentence_transformer_cls: Any | None = None
        self._model: Any | None = None
        self._index: Any | None = None
        self._metadata: list[ChunkMetadata] = []

    @property
    def faiss(self) -> Any:
        if self._faiss is None:
            try:
                import faiss
            except ImportError as exc:
                raise RAGVectorDependencyError(VECTOR_DEPENDENCY_HINT) from exc
            self._faiss = faiss
        return self._faiss

    @property
    def sentence_transformer_cls(self) -> Any:
        if self._sentence_transformer_cls is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RAGVectorDependencyError(VECTOR_DEPENDENCY_HINT) from exc
            self._sentence_transformer_cls = SentenceTransformer
        return self._sentence_transformer_cls

    @property
    def model(self) -> Any:
        if self._model is None:
            logger.info(
                "Loading embedding model: %s on %s",
                self.settings.embedding_model,
                self.settings.embedding_device,
            )
            self._model = self.sentence_transformer_cls(
                self.settings.embedding_model,
                device=self.settings.embedding_device,
            )
        return self._model

    def build_full_index(self, force: bool = False) -> int:
        """Scan all parsed JSONs and build a fresh vector index."""
        if not force and self.faiss_path.exists():
            logger.info("Index already exists. Use force=True to rebuild.")
            return 0

        self.index_dir.mkdir(parents=True, exist_ok=True)
        chunks: list[ChunkMetadata] = []

        json_dir = self.settings.parsed_output_dir
        json_files = list(json_dir.glob("**/*.json"))

        for json_path in json_files:
            try:
                with open(json_path, encoding="utf-8") as f:
                    data = json.load(f)

                doi = data.get("doi", "unknown")
                title = data.get("title", json_path.stem)
                content = data.get("content", "")

                if not content:
                    continue

                # Simple chunking by paragraphs for now
                paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
                for i, p in enumerate(paragraphs):
                    chunks.append(
                        ChunkMetadata(
                            doi=doi,
                            title=title,
                            chunk_index=i,
                            text_content=p,
                        )
                    )

            except Exception as e:
                logger.error(f"Failed to process {json_path}: {e}")
                continue

        if not chunks:
            logger.warning("No content found to index.")
            return 0

        logger.info(f"Generating embeddings for {len(chunks)} chunks...")
        texts = [c.text_content for c in chunks]
        faiss = self.faiss

        # Batch processing
        embeddings = self.model.encode(
            texts,
            batch_size=self.settings.embedding_batch_size,
            show_progress_bar=True,
            convert_to_numpy=True,
        )

        dimension = embeddings.shape[1]
        # Inner Product for cosine similarity with normalized vectors.
        index = faiss.IndexFlatIP(dimension)
        faiss.normalize_L2(embeddings)
        index.add(embeddings)

        faiss.write_index(index, str(self.faiss_path))
        with open(self.metadata_path, "wb") as f:
            pickle.dump(chunks, f)

        self._index = index
        self._metadata = chunks

        return len(chunks)

    def incremental_index(self, json_path: Path) -> int:
        """Add a single document to the existing index."""
        if not self.faiss_path.exists():
            return self.build_full_index()

        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)

            doi = data.get("doi", "unknown")
            title = data.get("title", json_path.stem)
            content = data.get("content", "")

            if not content:
                return 0

            paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
            new_chunks = []
            for i, p in enumerate(paragraphs):
                new_chunks.append(
                    ChunkMetadata(
                        doi=doi,
                        title=title,
                        chunk_index=i,
                        text_content=p,
                    )
                )

            texts = [c.text_content for c in new_chunks]
            embeddings = self.model.encode(texts, convert_to_numpy=True)
            faiss = self.faiss
            faiss.normalize_L2(embeddings)

            index = faiss.read_index(str(self.faiss_path))
            index.add(embeddings)

            faiss.write_index(index, str(self.faiss_path))

            with open(self.metadata_path, "rb") as f:
                metadata = pickle.load(f)

            metadata.extend(new_chunks)

            with open(self.metadata_path, "wb") as f:
                pickle.dump(metadata, f)

            return len(new_chunks)

        except Exception as e:
            logger.error(f"Incremental indexing failed for {json_path}: {e}")
            return 0

    def load_index(self) -> bool:
        """Load index and metadata into memory."""
        if not self.faiss_path.exists() or not self.metadata_path.exists():
            return False

        try:
            self._index = self.faiss.read_index(str(self.faiss_path))
            with open(self.metadata_path, "rb") as f:
                self._metadata = pickle.load(f)
            return True
        except Exception as e:
            logger.error(f"Failed to load index: {e}")
            return False
