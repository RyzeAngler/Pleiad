"""Deterministic BM25 search for Tool and Skill descriptors."""

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import log
from typing import Protocol


class SearchableDescriptor(Protocol):
    """The public metadata shared by Tool and Skill descriptors."""

    @property
    def name(self) -> str:
        """Return the capability display name."""
        ...

    @property
    def code(self) -> str:
        """Return the stable capability identity."""
        ...

    @property
    def description(self) -> str:
        """Return searchable capability metadata."""
        ...


K1 = 1.2
B = 0.75
FIELD_WEIGHTS: Mapping[str, float] = {
    "name": 4.0,
    "description": 2.0,
    "code": 1.0,
}
LATIN_OR_DIGIT = re.compile(r"[A-Za-z0-9_]+")
CJK = re.compile(r"[\u4e00-\u9fff]+")


@dataclass(frozen=True, slots=True)
class Document[DescriptorT: SearchableDescriptor]:
    """One immutable descriptor projection held by the search index."""

    descriptor: DescriptorT
    fields: Mapping[str, tuple[str, ...]]
    term_frequencies: Mapping[str, Mapping[str, int]]


class BM25Index[DescriptorT: SearchableDescriptor]:
    """Rank descriptors with a small, in-memory, field-weighted BM25 index.

    The index is intentionally rebuilt from a validated registry snapshot instead
    of being persisted separately. Capability catalogs are already dynamic,
    and rebuilding this metadata-only index on a catalog replacement keeps the
    search view consistent with the descriptors and avoids a second storage
    lifecycle for a derived artifact.
    """

    def __init__(self, descriptors: Iterable[DescriptorT]) -> None:
        self.documents: tuple[Document[DescriptorT], ...] = tuple(
            build_document(descriptor) for descriptor in descriptors
        )
        self.postings = self.build_postings()
        self.document_frequency = {
            field: {term: len(document_ids) for term, document_ids in terms.items()}
            for field, terms in self.postings.items()
        }
        self.average_field_lengths = self.build_average_field_lengths()

    def search(self, query: str = "", limit: int = 20) -> tuple[DescriptorT, ...]:
        """Return the highest-scoring descriptors in deterministic order."""
        if limit < 1:
            return ()
        terms = tuple(dict.fromkeys(tokenize(query)))
        if not terms:
            descriptors = sorted(
                (document.descriptor for document in self.documents),
                key=descriptor_sort_key,
            )
            return tuple(descriptors[:limit])

        candidate_ids = {
            document_id
            for field in FIELD_WEIGHTS
            for term in terms
            for document_id in self.postings[field].get(term, ())
        }
        ranked = [
            (
                self.score(self.documents[document_id], terms),
                self.documents[document_id].descriptor,
            )
            for document_id in sorted(candidate_ids)
        ]
        ranked = [item for item in ranked if item[0] > 0.0]
        ranked.sort(key=lambda item: (-item[0], *descriptor_sort_key(item[1])))
        return tuple(descriptor for _, descriptor in ranked[:limit])

    def score(
        self,
        document: Document[DescriptorT],
        terms: Sequence[str],
    ) -> float:
        score = 0.0
        for field, weight in FIELD_WEIGHTS.items():
            tokens = document.fields[field]
            if not tokens:
                continue
            average_length = self.average_field_lengths[field]
            normalization = B * len(tokens) / average_length if average_length else 0.0
            for term in terms:
                frequency = document.term_frequencies[field].get(term, 0)
                if not frequency:
                    continue
                score += weight * self.term_score(
                    field,
                    term,
                    frequency,
                    normalization,
                )
        return score

    def term_score(
        self,
        field: str,
        term: str,
        frequency: int,
        normalization: float,
    ) -> float:
        document_count = len(self.documents)
        document_frequency = self.document_frequency[field][term]
        inverse_frequency = log(1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5))
        return inverse_frequency * (frequency * (K1 + 1.0) / (frequency + K1 * (1.0 - B + normalization)))

    def build_postings(self) -> dict[str, dict[str, tuple[int, ...]]]:
        """Build field-specific postings lists for candidate retrieval."""
        postings: dict[str, dict[str, list[int]]] = {field: {} for field in FIELD_WEIGHTS}
        for document_id, document in enumerate(self.documents):
            for field, frequencies in document.term_frequencies.items():
                for term in frequencies:
                    postings[field].setdefault(term, []).append(document_id)
        return {
            field: {term: tuple(document_ids) for term, document_ids in terms.items()}
            for field, terms in postings.items()
        }

    def build_average_field_lengths(self) -> dict[str, float]:
        document_count = len(self.documents) or 1
        return {
            field: sum(len(document.fields[field]) for document in self.documents) / document_count
            for field in FIELD_WEIGHTS
        }


def tokenize(value: str) -> tuple[str, ...]:
    """Tokenize mixed Chinese, Latin, numeric, and namespaced code text."""
    tokens: list[str] = []
    for match in LATIN_OR_DIGIT.finditer(value.casefold()):
        token = match.group()
        tokens.append(token)
        if "_" in token:
            tokens.extend(part for part in token.split("_") if part)
    for match in CJK.finditer(value):
        text = match.group()
        if len(text) == 1:
            tokens.append(text)
            continue
        tokens.extend(text[index : index + 2] for index in range(len(text) - 1))
    return tuple(tokens)


def build_document[DescriptorT: SearchableDescriptor](descriptor: DescriptorT) -> Document[DescriptorT]:
    fields = descriptor_fields(descriptor)
    return Document(
        descriptor=descriptor,
        fields=fields,
        term_frequencies={field: dict(Counter(tokens)) for field, tokens in fields.items()},
    )


def descriptor_fields(descriptor: SearchableDescriptor) -> dict[str, tuple[str, ...]]:
    """Project any Tool- or Skill-shaped descriptor into indexed fields."""
    return {
        "name": tokenize(descriptor.name),
        "description": tokenize(descriptor.description),
        "code": tokenize(descriptor.code),
    }


def descriptor_sort_key(descriptor: SearchableDescriptor) -> tuple[str, str]:
    """Return a stable presentation order for a searchable descriptor."""
    return descriptor.name.casefold(), descriptor.code
