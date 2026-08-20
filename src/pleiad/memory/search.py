"""Deterministic local retrieval for durable Memory records."""

import re
from collections.abc import Sequence

from pleiad.memory.model import LongTermMemory, MemoryCandidate


class LexicalMemorySearch:
    """Rank Memory records with bounded multilingual token overlap."""

    def search(
        self,
        records: Sequence[LongTermMemory],
        query: str,
        *,
        limit: int,
    ) -> tuple[MemoryCandidate, ...]:
        """Return recent records sharing terms with the query."""
        terms = _terms(query)
        if not terms or limit <= 0:
            return ()

        ranked = [(self._score(record, terms), record) for record in records]
        return tuple(
            _candidate(record, score)
            for score, record in sorted(
                ranked,
                key=lambda pair: (pair[0], pair[1].updated_at),
                reverse=True,
            )
            if score > 0
        )[:limit]

    @staticmethod
    def _score(record: LongTermMemory, query_terms: frozenset[str]) -> float:
        """Prefer records matching more unique query terms."""
        record_terms = _terms(record.content)
        return float(len(query_terms & record_terms))


def _candidate(record: LongTermMemory, score: float) -> MemoryCandidate:
    """Project a durable record into a retrieval result."""
    return MemoryCandidate(
        memory_id=record.memory_id,
        scope=record.scope,
        content=record.content,
        kind=record.kind,
        source_message_ids=record.source_message_ids,
        score=score,
        updated_at=record.updated_at,
        metadata=dict(record.metadata),
    )


def _terms(value: str) -> frozenset[str]:
    """Create lightweight multilingual terms for local search."""
    return frozenset(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", value.casefold()))
