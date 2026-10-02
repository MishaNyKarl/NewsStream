from app.errors import UserError as UserError
from dataclasses import dataclass
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field

class StoryExtraction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=3, max_length=160)
    short_summary: str = Field(min_length=5, max_length=1000)
    current_state: str = Field(min_length=5, max_length=2500)
    entities: list[str] = Field(max_length=12)
    keywords: list[str] = Field(min_length=1, max_length=12)
    search_queries: list[str] = Field(min_length=2, max_length=5)
    watch_goals: list[str] = Field(min_length=1, max_length=5)

class Analysis(BaseModel):
    model_config = ConfigDict(extra='forbid')
    relevant: bool
    meaningful_update: bool
    novelty_score: float = Field(ge=0, le=1)
    importance_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    new_facts: list[str] = Field(max_length=3)
    updated_state: str = Field(max_length=3000)
    reason: str = Field(max_length=600)
    notification_summary: str = Field(max_length=1200)
    source_urls: list[str] = Field(max_length=6)


class FactEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fact_index: int = Field(ge=0, le=2)
    passage_id: str = Field(pattern=r'^s\d+p\d+$', max_length=40)


class ReviewedAnalysis(Analysis):
    evidence: list[FactEvidence] = Field(max_length=6)

@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str = ''
    published_at: datetime | None = None
    query: str = ''
    publisher_domain: str = ''


class SearchResults(list):
    """Results and per-request coverage, including partial provider failures."""
    def __init__(self, items=(), *, providers=(), failed_providers=()):
        super().__init__(items)
        self.providers = tuple(providers)
        self.failed_providers = tuple(failed_providers)

@dataclass
class FetchedContent:
    url: str
    title: str
    text: str

@dataclass
class Candidate:
    url: str
    normalized_url: str
    domain: str
    title: str
    content_excerpt: str
    content_hash: str
    search_query: str = ''
    published_at: datetime | None = None
    full_text: bool = True



@dataclass
class InterestSaveResult:
    interest: object
    created: bool
    monitoring_status: str

class ProviderUnavailable(UserError):
    pass
