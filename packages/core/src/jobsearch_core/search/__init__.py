from jobsearch_core.search.client import (
    BraveSearchClient,
    DdgsSearchClient,
    SearchClient,
    SearchError,
    SearchResult,
    SearXNGSearchClient,
    build_search_client,
)
from jobsearch_core.search.discover import discover, review_discoveries
from jobsearch_core.search.templates import (
    COUNTRIES,
    build_company_segment_queries,
    build_queries,
)

__all__ = [
    "COUNTRIES",
    "BraveSearchClient",
    "DdgsSearchClient",
    "SearchClient",
    "SearchError",
    "SearchResult",
    "SearXNGSearchClient",
    "build_company_segment_queries",
    "build_queries",
    "build_search_client",
    "discover",
    "review_discoveries",
]
