from __future__ import annotations


def resolve_publisher_url(doi: str | None, candidate_url: str | None) -> str | None:
    normalized_doi = normalize_doi(doi)
    if normalized_doi:
        return f"https://doi.org/{normalized_doi}"
    if not candidate_url:
        return None
    normalized_url = candidate_url.strip()
    return normalized_url or None


def normalize_doi(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    normalized = normalized.removeprefix("https://doi.org/")
    normalized = normalized.removeprefix("http://doi.org/")
    normalized = normalized.removeprefix("http://dx.doi.org/")
    normalized = normalized.removeprefix("https://dx.doi.org/")
    normalized = normalized.removeprefix("doi:")
    normalized = normalized.strip()
    return normalized.lower() or None
