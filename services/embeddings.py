"""
Text embedding service for semantic search.

This module handles:
- Chunking text into appropriate segments
- Generating embeddings using OpenAI's text-embedding-3-small
- Preparing vectors for storage in Supabase pgvector

The embedding dimension must match the pgvector column configuration
(default: 1536 for text-embedding-3-small).

Usage:
    from services.embeddings import embedding_service

    # Embed a single text
    vector = await embedding_service.embed_text("What is CropSight?")

    # Chunk and embed a transcript
    chunks = await embedding_service.chunk_and_embed_transcript(
        transcript="...",
        meeting_id="uuid"
    )
"""

import logging
import os
import re
from collections import Counter
from typing import Any

from openai import AsyncOpenAI

from config.settings import settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """
    Service for generating text embeddings.

    Uses OpenAI's text-embedding-3-small by default.
    Handles text chunking with speaker and timestamp awareness.
    """

    def __init__(self):
        """
        Initialize the embedding service with API credentials.

        Uses lazy initialization - client is created on first use.
        """
        self._client: AsyncOpenAI | None = None
        self.model = settings.EMBEDDING_MODEL
        self.dimension = settings.EMBEDDING_DIMENSION

    @property
    def client(self) -> AsyncOpenAI:
        """Get or create the OpenAI async client."""
        if self._client is None:
            api_key = settings.EMBEDDING_API_KEY or settings.OPENAI_API_KEY
            if not api_key:
                raise ValueError("EMBEDDING_API_KEY not configured")
            self._client = AsyncOpenAI(api_key=api_key)
        return self._client

    async def health_check(self) -> bool:
        """
        Verify the embedding service is working.

        Sends a small test embedding request.

        Returns:
            True if the service responds correctly, False otherwise.
        """
        try:
            result = await self.embed_text("health check")
            return len(result) == self.dimension
        except Exception as e:
            logger.warning(f"Embedding health check failed: {e}")
            return False

    # =========================================================================
    # Embedding Generation
    # =========================================================================

    async def embed_text(self, text: str) -> list[float]:
        """
        Generate an embedding vector for a single text.

        Args:
            text: The text to embed.

        Returns:
            List of floats representing the embedding vector (1536 dimensions).
        """
        if not text or not text.strip():
            raise ValueError("Cannot embed empty text")

        # Clean and truncate text if needed (model has 8191 token limit)
        cleaned_text = self._clean_text(text)

        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=cleaned_text,
                dimensions=self.dimension
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"Error generating embedding: {e}")
            raise

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts in a batch.

        More efficient than calling embed_text multiple times.
        OpenAI supports batch embedding in a single API call.

        Args:
            texts: List of texts to embed.

        Returns:
            List of embedding vectors (one per input text).
        """
        if not texts:
            return []

        # Clean all texts
        cleaned_texts = [self._clean_text(t) for t in texts if t and t.strip()]

        if not cleaned_texts:
            return []

        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=cleaned_texts,
                dimensions=self.dimension
            )
            # Sort by index to maintain order
            sorted_data = sorted(response.data, key=lambda x: x.index)
            return [item.embedding for item in sorted_data]
        except Exception as e:
            logger.error(f"Error generating batch embeddings: {e}")
            raise

    # =========================================================================
    # Text Chunking
    # =========================================================================

    def chunk_transcript(
        self,
        transcript: str,
        chunk_size: int = 1000,
        overlap: int = 200
    ) -> list[dict]:
        """
        Split a transcript into overlapping chunks for embedding.

        Attempts to split on speaker changes and paragraph boundaries
        for more coherent chunks.

        Tactiq format: [HH:MM:SS] Speaker: Text

        Args:
            transcript: The full transcript text.
            chunk_size: Target size for each chunk (in characters).
            overlap: Number of overlapping characters between chunks.

        Returns:
            List of chunk dicts with:
            - text: The chunk content
            - chunk_index: Position in the transcript
            - speaker: Primary speaker in this chunk (if detectable)
            - timestamp_range: Approximate timestamp range
        """
        if not transcript or not transcript.strip():
            return []

        # Parse transcript into utterances
        utterances = self._parse_utterances(transcript)

        if not utterances:
            # Fallback: treat as plain text
            return self._chunk_plain_text(transcript, chunk_size, overlap)

        # Group utterances into chunks
        chunks = []
        current_chunk_text = ""
        current_chunk_utterances = []
        chunk_index = 0

        for utterance in utterances:
            utterance_text = self._format_utterance(utterance)

            # Check if adding this utterance exceeds chunk size
            if len(current_chunk_text) + len(utterance_text) > chunk_size:
                # Save current chunk if not empty
                if current_chunk_text:
                    chunks.append(self._create_chunk(
                        text=current_chunk_text.strip(),
                        chunk_index=chunk_index,
                        utterances=current_chunk_utterances
                    ))
                    chunk_index += 1

                    # Start new chunk with overlap
                    overlap_utterances = self._get_overlap_utterances(
                        current_chunk_utterances, overlap
                    )
                    current_chunk_text = "".join(
                        self._format_utterance(u) for u in overlap_utterances
                    )
                    current_chunk_utterances = list(overlap_utterances)

            current_chunk_text += utterance_text
            current_chunk_utterances.append(utterance)

        # Save final chunk
        if current_chunk_text.strip():
            chunks.append(self._create_chunk(
                text=current_chunk_text.strip(),
                chunk_index=chunk_index,
                utterances=current_chunk_utterances
            ))

        return chunks

    def chunk_document(
        self,
        document: str,
        chunk_size: int = 1000,
        overlap: int = 200
    ) -> list[dict]:
        """
        Split a document into overlapping chunks for embedding.

        Attempts to split on paragraph and section boundaries.

        Args:
            document: The document text content.
            chunk_size: Target size for each chunk (in characters).
            overlap: Number of overlapping characters between chunks.

        Returns:
            List of chunk dicts with:
            - text: The chunk content
            - chunk_index: Position in the document
        """
        return self._chunk_plain_text(document, chunk_size, overlap)

    def _chunk_plain_text(
        self,
        text: str,
        chunk_size: int,
        overlap: int
    ) -> list[dict]:
        """
        Chunk plain text using paragraph boundaries.

        Args:
            text: Text to chunk.
            chunk_size: Target chunk size.
            overlap: Overlap between chunks.

        Returns:
            List of chunk dicts.
        """
        if not text or not text.strip():
            return []

        # Split on double newlines (paragraphs)
        paragraphs = re.split(r'\n\s*\n', text)
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        chunks = []
        current_chunk = ""
        chunk_index = 0

        for paragraph in paragraphs:
            # If paragraph alone exceeds chunk_size, split it further
            if len(paragraph) > chunk_size:
                # Split on sentences
                sentences = re.split(r'(?<=[.!?])\s+', paragraph)
                for sentence in sentences:
                    if len(current_chunk) + len(sentence) + 1 > chunk_size:
                        if current_chunk:
                            chunks.append({
                                "text": current_chunk.strip(),
                                "chunk_index": chunk_index
                            })
                            chunk_index += 1
                            # Get overlap from end of current chunk
                            current_chunk = current_chunk[-overlap:] if overlap > 0 else ""
                    current_chunk += " " + sentence
            else:
                if len(current_chunk) + len(paragraph) + 2 > chunk_size:
                    if current_chunk:
                        chunks.append({
                            "text": current_chunk.strip(),
                            "chunk_index": chunk_index
                        })
                        chunk_index += 1
                        current_chunk = current_chunk[-overlap:] if overlap > 0 else ""
                current_chunk += "\n\n" + paragraph

        # Save final chunk
        if current_chunk.strip():
            chunks.append({
                "text": current_chunk.strip(),
                "chunk_index": chunk_index
            })

        return chunks

    # =========================================================================
    # Combined Operations
    # =========================================================================

    async def chunk_and_embed_transcript(
        self,
        transcript: str,
        meeting_id: str
    ) -> list[dict]:
        """
        Chunk a transcript and generate embeddings for each chunk.

        Args:
            transcript: The full transcript text.
            meeting_id: UUID of the meeting (for metadata).

        Returns:
            List of chunk dicts with embeddings included:
            - text: The chunk content
            - embedding: The vector embedding
            - chunk_index: Position in the transcript
            - speaker: Primary speaker (if detectable)
            - timestamp_range: Approximate timestamps
            - metadata: Additional context
        """
        # Chunk the transcript
        chunks = self.chunk_transcript(transcript)

        if not chunks:
            logger.warning(f"No chunks generated for meeting {meeting_id}")
            return []

        # Extract texts for batch embedding
        texts = [chunk["text"] for chunk in chunks]

        # Generate embeddings in batch
        embeddings = await self.embed_texts(texts)

        # Combine chunks with embeddings
        result = []
        for chunk, embedding in zip(chunks, embeddings):
            result.append({
                "text": chunk["text"],
                "embedding": embedding,
                "chunk_index": chunk["chunk_index"],
                "speaker": chunk.get("speaker"),
                "timestamp_range": chunk.get("timestamp_range"),
                "metadata": {
                    "meeting_id": meeting_id,
                    "source_type": "meeting"
                }
            })

        logger.info(
            f"Generated {len(result)} embedded chunks for meeting {meeting_id}"
        )
        return result

    async def chunk_and_embed_transcript_with_context(
        self,
        transcript: str,
        meeting_id: str,
        meeting_title: str,
        meeting_date: str,
        participants: list[str],
    ) -> list[dict]:
        """
        Chunk a transcript and generate embeddings enriched with meeting context.

        Similar to chunk_and_embed_transcript(), but prepends meeting metadata
        to each chunk ONLY for the embedding step (so the vector captures
        who/when/what). The stored chunk_text stays raw for display.

        Args:
            transcript: The full transcript text.
            meeting_id: UUID of the meeting (for metadata).
            meeting_title: Title of the meeting (e.g., "MVP Focus").
            meeting_date: Date string (e.g., "2026-02-22").
            participants: List of participant names.

        Returns:
            List of chunk dicts with embeddings included:
            - text: The raw chunk content (for display)
            - embedding: The vector embedding (context-enriched)
            - chunk_index: Position in the transcript
            - speaker: Primary speaker (if detectable)
            - timestamp_range: Approximate timestamps
            - metadata: Includes context_prefix and meeting info
        """
        # Chunk the transcript (same chunking as the non-contextual method)
        chunks = self.chunk_transcript(transcript)

        if not chunks:
            logger.warning(f"No chunks generated for meeting {meeting_id}")
            return []

        # Build the context prefix — this gets prepended for embedding only
        context_prefix = (
            f"Meeting: {meeting_title} | "
            f"Date: {meeting_date} | "
            f"Participants: {', '.join(participants)}\n\n"
        )

        # For embedding: prepend the context prefix to each chunk's text
        # so the vector captures meeting context (who, when, what)
        texts_for_embedding = [context_prefix + chunk["text"] for chunk in chunks]

        # Generate embeddings in batch (context-enriched text)
        embeddings = await self.embed_texts(texts_for_embedding)

        # Combine chunks with embeddings — store raw text, not the prefixed version
        result = []
        for chunk, embedding in zip(chunks, embeddings):
            result.append({
                "text": chunk["text"],              # raw text for display
                "embedding": embedding,             # context-enriched embedding
                "chunk_index": chunk["chunk_index"],
                "speaker": chunk.get("speaker"),
                "timestamp_range": chunk.get("timestamp_range"),
                "metadata": {
                    "meeting_id": meeting_id,
                    "source_type": "meeting",
                    "context_prefix": context_prefix,
                    "meeting_title": meeting_title,
                    "meeting_date": meeting_date,
                    "participants": participants,
                }
            })

        logger.info(
            f"Generated {len(result)} context-enriched embedded chunks "
            f"for meeting {meeting_id}"
        )
        return result

    async def chunk_and_embed_document(
        self,
        document: str,
        document_id: str
    ) -> list[dict]:
        """
        Chunk a document and generate embeddings for each chunk.

        Args:
            document: The document text content.
            document_id: UUID of the document (for metadata).

        Returns:
            List of chunk dicts with embeddings included.
        """
        # Chunk the document
        chunks = self.chunk_document(document)

        if not chunks:
            logger.warning(f"No chunks generated for document {document_id}")
            return []

        # Extract texts for batch embedding
        texts = [chunk["text"] for chunk in chunks]

        # Generate embeddings in batch
        embeddings = await self.embed_texts(texts)

        # Combine chunks with embeddings
        result = []
        for chunk, embedding in zip(chunks, embeddings):
            result.append({
                "text": chunk["text"],
                "embedding": embedding,
                "chunk_index": chunk["chunk_index"],
                "speaker": None,
                "timestamp_range": None,
                "metadata": {
                    "document_id": document_id,
                    "source_type": "document"
                }
            })

        logger.info(
            f"Generated {len(result)} embedded chunks for document {document_id}"
        )
        return result

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _clean_text(self, text: str) -> str:
        """
        Clean text for embedding.

        Removes excessive whitespace and truncates if necessary.
        """
        # Remove excessive whitespace
        cleaned = re.sub(r'\s+', ' ', text).strip()

        # Truncate to approximate token limit (rough: 4 chars per token)
        max_chars = 8000 * 4  # ~8000 tokens, leaving buffer
        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars]
            logger.warning(f"Truncated text from {len(text)} to {max_chars} chars")

        return cleaned

    def _parse_utterances(self, transcript: str) -> list[dict]:
        """
        Parse a Tactiq transcript into utterances.

        Handles both formats:
        - Bracketed:   [HH:MM:SS] Speaker: Text
        - Unbracketed: MM:SS Speaker: Text

        Args:
            transcript: Raw transcript text.

        Returns:
            List of dicts with speaker, timestamp, and text.
        """
        # Try bracketed format first: [HH:MM:SS] or [MM:SS]
        pattern_bracketed = r'\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([^:\[\]]+):\s*(.+?)(?=\n\[|\Z)'
        matches = re.findall(pattern_bracketed, transcript, re.DOTALL)

        if not matches:
            # Fall back to unbracketed Tactiq format
            pattern_unbracketed = r'^(\d{1,2}:\d{2}(?::\d{2})?)\s+([^:\d][^:]+):\s*(.+?)(?=\n\d{1,2}:\d{2}|\Z)'
            matches = re.findall(pattern_unbracketed, transcript, re.DOTALL | re.MULTILINE)

        utterances = []
        for timestamp, speaker, text in matches:
            utterances.append({
                "timestamp": timestamp.strip(),
                "speaker": speaker.strip().title(),
                "text": text.strip()
            })

        return utterances

    def _format_utterance(self, utterance: dict) -> str:
        """Format an utterance for chunk text."""
        return f"[{utterance['timestamp']}] {utterance['speaker']}: {utterance['text']}\n"

    def _create_chunk(
        self,
        text: str,
        chunk_index: int,
        utterances: list[dict]
    ) -> dict:
        """Create a chunk dict with metadata from utterances."""
        return {
            "text": text,
            "chunk_index": chunk_index,
            "speaker": self._get_primary_speaker(utterances),
            "timestamp_range": self._get_timestamp_range(utterances)
        }

    def _get_primary_speaker(self, utterances: list[dict]) -> str | None:
        """Get the most frequent speaker in a list of utterances."""
        if not utterances:
            return None
        speakers = [u.get("speaker") for u in utterances if u.get("speaker")]
        if not speakers:
            return None
        counter = Counter(speakers)
        return counter.most_common(1)[0][0]

    def _get_timestamp_range(self, utterances: list[dict]) -> str | None:
        """Get timestamp range from first to last utterance."""
        if not utterances:
            return None

        timestamps = [u.get("timestamp") for u in utterances if u.get("timestamp")]
        if not timestamps:
            return None

        return f"{timestamps[0]}-{timestamps[-1]}"

    def _get_overlap_utterances(
        self,
        utterances: list[dict],
        target_chars: int
    ) -> list[dict]:
        """
        Get utterances from the end that total approximately target_chars.

        Used to create overlap between chunks.
        """
        if not utterances or target_chars <= 0:
            return []

        result = []
        total_chars = 0

        for utterance in reversed(utterances):
            utterance_text = self._format_utterance(utterance)
            if total_chars + len(utterance_text) > target_chars and result:
                break
            result.insert(0, utterance)
            total_chars += len(utterance_text)

        return result

    def _extract_speaker(self, chunk_text: str) -> str | None:
        """
        Extract the primary speaker from a chunk of transcript.

        Args:
            chunk_text: The chunk text to analyze.

        Returns:
            Speaker name if found, None otherwise.
        """
        # Find all speaker labels in format: "Speaker:"
        pattern = r'\]\s*([^:\[\]]+):'
        matches = re.findall(pattern, chunk_text)

        if not matches:
            return None

        # Return most common speaker
        counter = Counter(matches)
        return counter.most_common(1)[0][0].strip()

    def _extract_timestamp_range(self, chunk_text: str) -> str | None:
        """
        Extract the timestamp range from a chunk of transcript.

        Args:
            chunk_text: The chunk text to analyze.

        Returns:
            Timestamp range string (e.g., "43:00-45:30"), or None.
        """
        # Find all timestamps in format: [HH:MM:SS] or [MM:SS]
        pattern = r'\[(\d{1,2}:\d{2}(?::\d{2})?)\]'
        matches = re.findall(pattern, chunk_text)

        if not matches:
            return None

        return f"{matches[0]}-{matches[-1]}"


# Singleton instance
embedding_service = EmbeddingService()
