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
    new_facts: list[str] = Field(max_length=8)
    updated_state: str = Field(max_length=3000)
    reason: str = Field(max_length=600)
    notification_summary: str = Field(max_length=1200)
    source_urls: list[str] = Field(max_length=6)

@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str = ''
    published_at: datetime | None = None
    query: str = ''

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

class UserError(Exception):
    pass

class ProviderUnavailable(UserError):
    pass
