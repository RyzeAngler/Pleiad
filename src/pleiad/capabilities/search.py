"""Deterministic BM25 search for Tool and Skill descriptors."""

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import log
from typing import Protocol, cast


class SearchableDescriptor(Protocol):
    """The public metadata shared by Tool and Skill descriptors."""

    name: str
    code: str
    description: str


_K1 = 1.2
_B = 0.75
_FIELD_WEIGHTS: Mapping[str, float] = {
    "name": 4.0,
    "description": 2.0,
    "code": 1.0,
}
_LATIN_OR_DIGIT = re.compile(r"[A-Za-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")


@dataclass(frozen=True, slots=True)
class _Document[DescriptorT]:
    """One immutable descriptor projection held by the search index."""

    descriptor: DescriptorT
    fields: Mapping[str, tuple[str, ...]]
    term_frequencies: Mapping[str, Mapping[str, int]]


class BM25Index[DescriptorT]:
    """Rank descriptors with a small, in-memory, field-weighted BM25 index.

    The index is intentionally rebuilt from a validated Zoo snapshot instead
    of being persisted separately. Capability catalogs are already dynamic,
    and rebuilding this metadata-only index on a catalog replacement keeps the
    search view consistent with the descriptors and avoids a second storage
    lifecycle for a derived artifact.
    """

    def __init__(self, descriptors: Iterable[DescriptorT]) -> None:
        self._documents = tuple(_build_document(descriptor) for descriptor in descriptors)
        self._postings = self._build_postings()
        self._document_frequency = {
            field: {term: len(document_ids) for term, document_ids in terms.items()}
            for field, terms in self._postings.items()
        }
        self._average_field_lengths = self._build_average_field_lengths()

    def search(self, query: str = "", limit: int = 20) -> tuple[DescriptorT, ...]:
        """Return the highest-scoring descriptors in deterministic order."""
        if limit < 1:
            return ()
        terms = tuple(dict.fromkeys(_tokenize(query)))
        if not terms:
            descriptors = sorted(
                (document.descriptor for document in self._documents),
                key=_descriptor_sort_key,
            )
            return tuple(descriptors[:limit])

        candidate_ids = {
            document_id
            for field in _FIELD_WEIGHTS
            for term in terms
            for document_id in self._postings[field].get(term, ())
        }
        ranked = [
            (
                self._score(self._documents[document_id], terms),
                self._documents[document_id].descriptor,
            )
            for document_id in sorted(candidate_ids)
        ]
        ranked = [item for item in ranked if item[0] > 0.0]
        ranked.sort(key=lambda item: (-item[0], *_descriptor_sort_key(item[1])))
        return tuple(descriptor for _, descriptor in ranked[:limit])

    def _score(
        self,
        document: _Document[DescriptorT],
        terms: Sequence[str],
    ) -> float:
        score = 0.0
        for field, weight in _FIELD_WEIGHTS.items():
            tokens = document.fields[field]
            if not tokens:
                continue
            average_length = self._average_field_lengths[field]
            normalization = _B * len(tokens) / average_length if average_length else 0.0
            for term in terms:
                frequency = document.term_frequencies[field].get(term, 0)
                if not frequency:
                    continue
                score += weight * self._term_score(
                    field,
                    term,
                    frequency,
                    normalization,
                )
        return score

    def _term_score(
        self,
        field: str,
        term: str,
        frequency: int,
        normalization: float,
    ) -> float:
        document_count = len(self._documents)
        document_frequency = self._document_frequency[field][term]
        inverse_frequency = log(
            1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
        )
        return inverse_frequency * (
            frequency * (_K1 + 1.0) / (frequency + _K1 * (1.0 - _B + normalization))
        )

    def _build_postings(self) -> dict[str, dict[str, tuple[int, ...]]]:
        """Build field-specific postings lists for candidate retrieval."""
        postings: dict[str, dict[str, list[int]]] = {field: {} for field in _FIELD_WEIGHTS}
        for document_id, document in enumerate(self._documents):
            for field, frequencies in document.term_frequencies.items():
                for term in frequencies:
                    postings[field].setdefault(term, []).append(document_id)
        return {
            field: {term: tuple(document_ids) for term, document_ids in terms.items()}
            for field, terms in postings.items()
        }

    def _build_average_field_lengths(self) -> dict[str, float]:
        document_count = len(self._documents) or 1
        return {
            field: sum(len(document.fields[field]) for document in self._documents) / document_count
            for field in _FIELD_WEIGHTS
        }


def _tokenize(value: str) -> tuple[str, ...]:
    """Tokenize mixed Chinese, Latin, numeric, and namespaced code text."""
    tokens: list[str] = []
    for match in _LATIN_OR_DIGIT.finditer(value.casefold()):
        token = match.group()
        tokens.append(token)
        if "_" in token:
            tokens.extend(part for part in token.split("_") if part)
    for match in _CJK.finditer(value):
        text = match.group()
        if len(text) == 1:
            tokens.append(text)
            continue
        tokens.extend(text[index : index + 2] for index in range(len(text) - 1))
    return tuple(tokens)


def _build_document[DescriptorT](descriptor: DescriptorT) -> _Document[DescriptorT]:
    fields = _descriptor_fields(descriptor)
    return _Document(
        descriptor=descriptor,
        fields=fields,
        term_frequencies={field: dict(Counter(tokens)) for field, tokens in fields.items()},
    )


def _descriptor_fields(descriptor: object) -> dict[str, tuple[str, ...]]:
    """Project any Tool- or Skill-shaped descriptor into indexed fields."""
    searchable = cast(SearchableDescriptor, descriptor)
    return {
        "name": _tokenize(searchable.name),
        "description": _tokenize(searchable.description),
        "code": _tokenize(searchable.code),
    }


def _descriptor_sort_key(descriptor: object) -> tuple[str, str]:
    """Return a stable presentation order for a searchable descriptor."""
    searchable = cast(SearchableDescriptor, descriptor)
    return searchable.name.casefold(), searchable.code
