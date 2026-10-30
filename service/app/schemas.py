"""Request and response shapes for the API. FastAPI turns these into the /docs page and validates every request."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class Client(BaseModel):
    name: str = Field(min_length=1, max_length=200, examples=["Example Landscapes"])
    domain: str = Field(default="", max_length=300, examples=["example-landscapes.co.uk"])
    notes: str = Field(default="", max_length=5000, description="Audience, tone and rules for this client")
    priority_pages: list[HttpUrl] = Field(default_factory=list, max_length=100, description="Service or money pages to favour")


class Post(BaseModel):
    title: str = Field(default="", max_length=500)
    keyword: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=2000, description="Planned URL, so the post never links to itself")
    content: str = Field(min_length=1, max_length=200_000, description="Plain text, Markdown or HTML")


class Options(BaseModel):
    effort: Literal["low", "medium", "high"] | None = Field(
        None, description="How much the model thinks before answering. Lower is faster. Leave out to use the service default.")
    provider: Literal["anthropic", "gemini", "claude-vertex"] | None = Field(
        None, description="Which model to use for this request: Claude (Anthropic API), Gemini (Vertex AI) or Claude (Vertex AI). "
                          "Leave out to use the service default, with the automatic fallback. Naming one turns the fallback off.")
    max_seconds: int | None = Field(None, ge=10, le=300, description="Finish within about this many seconds: shorter model time "
        "limits, and optional extra steps skipped if time runs short. For callers with a hard limit, like the Google Doc side panel (30s).")
    force_fallback: bool = Field(False, description="Testing only (API key callers): act as if the main model failed, to check the fallback.")


class GenerateRequest(BaseModel):
    client: Client
    post: Post
    sitemap_urls: list[str] = Field(default_factory=list, max_length=100_000,
                                    description="The client's page URLs. Leave empty to use the pages in the client's index (see POST /index).")
    options: Options = Field(default_factory=Options)


class Meta(BaseModel):
    text: str
    angle: str = ""
    length: int
    status: Literal["short", "good", "may truncate"]


class Link(BaseModel):
    anchor_text: str
    sentence: str
    target_url: str
    reason: str = ""
    priority: Literal["high", "medium", "low"] = "medium"
    verified: bool = True


class Backlink(BaseModel):
    source_url: str
    anchor_text: str = ""
    reason: str = ""


class Heading(BaseModel):
    text: str
    level: int = Field(ge=1, le=6)


class ListBlock(BaseModel):
    type: Literal["ul", "ol"] = "ul"
    items: list[str]


class Stats(BaseModel):
    candidates: int = Field(description="Sitemap URLs sent to the model")
    suggested: int = Field(description="Links the model suggested first time")
    verified: int
    dropped: int
    retried: int = Field(description="Links sent back to the model for an exact anchor")
    model: str
    provider: str | None = Field(None, description="The model that answered: anthropic, gemini or claude-vertex")
    fell_back: str | None = Field(None, description="Set when the main model failed and the fallback answered, with the reason")
    effort: str | None = Field(None, description="Effort level used for the main model call")
    retrieval: Literal["semantic", "url"] = Field("url", description="How link candidates were chosen")
    indexed_pages: int = Field(0, description="Pages in the client's index, when retrieval is semantic")
    search_console: dict | None = Field(None, description="Ranking data used: the property, how many close-to-page-one pages were "
                                                          "offered and linked, or why it couldn't be used")
    link_mix: dict[str, int] = Field(default_factory=dict, description="Kept links by page type: priority, main_service, service, "
                                                                     "contact, article, other, plus how many are close to page one")
    main_link_followup: str | None = Field(None, description="Set when the first answer had no priority or main service link and the model "
                                                             "was asked again: added, nothing in the post fits, anchor not in the post, or failed")
    set_aside: list[dict] = Field(default_factory=list, description="Links the model suggested that weren't kept, with the page type and why")
    link_range: list[int] = Field(default_factory=list, description="How many links were asked for, from the post's length")
    trimmed: int = Field(0, description="Verified links dropped to stay within the range (lowest priority first)")
    timings: dict[str, int] = Field(default_factory=dict, description="Milliseconds per stage: retrieval, model, anchor fix-up")
    model_attempts: list[dict] = Field(default_factory=list, description="Each call to the model: how long it took and how it ended")
    ms: int


class GenerateResponse(BaseModel):
    run_id: str
    title_tag: str
    title_status: Literal["good", "may truncate"]
    primary_keyword: str
    meta_descriptions: list[Meta]
    internal_links: list[Link]
    link_back_from: list[Backlink]
    headings: list[Heading]
    lists: list[ListBlock]
    stats: Stats


class IndexRequest(BaseModel):
    client: Client
    sitemap_url: str = Field(default="", max_length=2000, description="The client's sitemap.xml (or sitemap index). Fetched by the service.",
                             examples=["https://www.example-landscapes.co.uk/sitemap.xml"])
    sitemap_urls: list[str] = Field(default_factory=list, max_length=100_000, description="Or the page URLs themselves")
    budget_s: float | None = Field(None, ge=10, le=240, description="Seconds to work for on this call (default about 200). "
                                                                    "Callers with a short time limit, such as Apps Script, can ask for less.")


class IndexStats(BaseModel):
    domain: str
    pages_in_sitemap: int
    indexed: int = Field(description="Pages in the index now")
    crawled_now: int
    embedded_now: int = Field(description="Pages embedded this call (new or changed)")
    unchanged: int
    removed: int = Field(description="Pages dropped because they left the sitemap")
    skipped: dict[str, int] = Field(description="Pages not indexed, by reason (noindex, http_404, robots_txt...)")
    skip_examples: dict[str, list[str]] = Field(default_factory=dict, description="Up to 3 example URLs per skip reason")
    retry_next_run: int = Field(0, description="Pages that failed for a temporary reason (timeout, rate limit) and will be retried")
    remaining: int = Field(description="Pages still to crawl, including temporary failures. Call again until this is 0.")
    stopped_early: str = Field("", description="Set when the site kept refusing and the crawl paused; try again later")
    model: str
    seconds: float
