import asyncio
from collections.abc import Sequence
from functools import cached_property

from app.config import settings

BATCH_SIZE = 100  # inputs per request


class OpenAIEmbedder:

    @cached_property
    def _client(self):  
        """ The OpenAI SDK client."""
        from openai import OpenAI

        return OpenAI(
            api_key=settings.openai_api_key,
            max_retries=3,
        )


    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
            """ Embed a batch of texts in a thread, returning the embedding vectors in the same order."""
    
            response = self._client.embeddings.create(model=settings.embedding_model, input=batch)
    
            return [item.embedding for item in response.data]
    

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """ Embed a batch of texts, returning the embedding vectors in the same order."""

        vectors: list[list[float]] = []

        for start in range(0, len(texts), BATCH_SIZE):
            batch = list(texts[start : start + BATCH_SIZE])
            vectors += await asyncio.to_thread(self._embed_batch, batch)

        return vectors
