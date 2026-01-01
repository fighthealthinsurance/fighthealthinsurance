"""Client for Magic RAG Service integration.

Provides an async client for calling the Magic RAG Service
to retrieve evidence-based context for insurance appeal generation.
"""

import time
from dataclasses import dataclass
from typing import Optional

import httpx
from loguru import logger

from fighthealthinsurance.utils import get_env_variable


@dataclass
class RAGContextResult:
    """Result from RAG service appeal context query."""

    denial_response: str
    medical_guidelines: str
    regulatory_requirements: str
    procedure_specific: str
    diagnosis_specific: str
    total_sources: int

    def get_combined_context(self) -> str:
        """Get all context combined into a single string."""
        parts = []
        if self.denial_response:
            parts.append(f"Denial Response Evidence:\n{self.denial_response}")
        if self.medical_guidelines:
            parts.append(f"Medical Guidelines:\n{self.medical_guidelines}")
        if self.regulatory_requirements:
            parts.append(f"Regulatory Requirements:\n{self.regulatory_requirements}")
        if self.procedure_specific:
            parts.append(f"Procedure-Specific Evidence:\n{self.procedure_specific}")
        if self.diagnosis_specific:
            parts.append(f"Diagnosis-Specific Evidence:\n{self.diagnosis_specific}")
        return "\n\n".join(parts)

    def is_empty(self) -> bool:
        """Check if the result contains any meaningful context."""
        return self.total_sources == 0


class RAGClient:
    """Async client for the Magic RAG Service."""

    # Cache health status for 60 seconds to avoid repeated checks
    _HEALTH_CACHE_TTL = 60.0

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
    ):
        """Initialize. Uses RAG_SERVICE_URL env var, defaulting to localhost:8001."""
        self.base_url = base_url or get_env_variable(
            "RAG_SERVICE_URL", "http://localhost:8001"
        )
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._health_ok: Optional[bool] = None
        self._health_checked_at: float = 0.0

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        """Check if the RAG service is healthy (cached for 60s)."""
        now = time.monotonic()
        if (
            self._health_ok is not None
            and (now - self._health_checked_at) < self._HEALTH_CACHE_TTL
        ):
            return self._health_ok
        try:
            client = await self._get_client()
            response = await client.get("/health/")
            self._health_ok = response.status_code == 200
        except Exception as e:
            logger.debug(f"RAG service health check failed: {e}")
            self._health_ok = False
        self._health_checked_at = now
        return self._health_ok

    async def get_appeal_context(
        self,
        denial_reason: str,
        procedure_codes: Optional[list[str]] = None,
        diagnosis_codes: Optional[list[str]] = None,
        state: Optional[str] = None,
        max_context_length: int = 8000,
    ) -> Optional[RAGContextResult]:
        """Get context for generating an insurance appeal."""
        try:
            client = await self._get_client()

            payload: dict = {
                "denial_reason": denial_reason,
                "max_context_length": max_context_length,
            }
            if procedure_codes:
                payload["procedure_codes"] = procedure_codes
            if diagnosis_codes:
                payload["diagnosis_codes"] = diagnosis_codes
            if state:
                payload["state"] = state

            logger.debug(f"Requesting RAG context with payload: {payload}")

            response = await client.post("/api/v1/appeal-context/", json=payload)

            if response.status_code != 200:
                logger.warning(
                    f"RAG service returned status {response.status_code}: {response.text}"
                )
                return None

            data = response.json()
            context = data.get("context", {})

            return RAGContextResult(
                denial_response=context.get("denial_response", ""),
                medical_guidelines=context.get("medical_guidelines", ""),
                regulatory_requirements=context.get("regulatory_requirements", ""),
                procedure_specific=context.get("procedure_specific", ""),
                diagnosis_specific=context.get("diagnosis_specific", ""),
                total_sources=data.get("total_sources", 0),
            )

        except httpx.TimeoutException:
            logger.warning("RAG service request timed out")
            return None
        except httpx.RequestError as e:
            logger.warning(f"RAG service request failed: {e}")
            return None
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Unexpected error calling RAG service: {e}"
            )
            return None


# Global client instance (lazy initialization)
_rag_client: Optional[RAGClient] = None


def get_rag_client() -> RAGClient:
    """Get the global RAG client instance."""
    global _rag_client
    if _rag_client is None:
        _rag_client = RAGClient()
    return _rag_client


async def get_rag_context_for_denial(
    denial_text: str,
    state: Optional[str] = None,
    procedure_codes: Optional[list[str]] = None,
    diagnosis_codes: Optional[list[str]] = None,
) -> Optional[str]:
    """Get RAG context for a denial.

    Main entry point for integrating RAG context into appeal generation.

    Returns:
        Combined context string for inclusion in appeal prompts,
        or None if RAG service is unavailable or returns no results.
    """
    client = get_rag_client()

    if not await client.health_check():
        logger.info("RAG service not available, skipping context enrichment")
        return None

    # Use first 500 chars as the denial reason query
    denial_reason = denial_text[:500] if denial_text else ""
    if not denial_reason:
        return None

    result = await client.get_appeal_context(
        denial_reason=denial_reason,
        procedure_codes=procedure_codes,
        diagnosis_codes=diagnosis_codes,
        state=state,
    )

    if result is None or result.is_empty():
        logger.debug("RAG service returned no context")
        return None

    context = result.get_combined_context()
    if context:
        logger.info(f"RAG service returned context from {result.total_sources} sources")
    return context
