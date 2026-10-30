import pytest
from fastapi.testclient import TestClient

from app.main import app, get_model

API_KEY = "test-key"

KNOWN = [
    "https://example-landscapes.co.uk/services/garden-design/",
    "https://example-landscapes.co.uk/services/patio-installation/",
    "https://example-landscapes.co.uk/services/artificial-grass/",
    "https://example-landscapes.co.uk/areas/watford/",
]

POST = """# Planning Your Dream Garden

A good garden starts with a plan. Our garden design service maps out every bed, path and seating area before a spade goes in.

## Choosing a Patio

Natural stone is hard wearing and looks great for decades. We handle patio installation from groundwork to pointing.

## Low Maintenance Options

- Artificial grass: stays green all year
- Gravel borders

Get in touch for a free survey in Watford.

## Planning Ahead

Spring is the busiest season for outdoor projects, so it pays to plan early. Think about how you use the space through the year, where the sun falls in the afternoon, and how much time you want to spend looking after it. """ + ("A bit of planning now saves time and money later, and it means the finished space suits the way you actually live. " * 18)

FIRST_ANSWER = {
    "title_tag": "Planning Your Dream Garden | Example Landscapes",
    "primary_keyword": "garden design",
    "meta_descriptions": [
        {"text": "x" * 150, "angle": "benefit-led"},
        {"text": "y" * 170, "angle": "question-led"},
        {"text": "short one", "angle": "keyword-first"},
        {"angle": "no text"},
    ],
    "internal_links": [
        {"anchor_text": "patio installation", "sentence": "", "target_url": KNOWN[1], "reason": "service page", "priority": "high"},
        {"anchor_text": "garden design service", "sentence": "", "target_url": KNOWN[0] + "?utm=x", "reason": "service page", "priority": "urgent"},
        {"anchor_text": "fake grass lawns", "sentence": "", "target_url": KNOWN[2], "reason": "close match", "priority": "medium"},
        {"anchor_text": "free survey", "sentence": "", "target_url": "https://competitor.com/survey/", "reason": "nope", "priority": "low"},
    ],
    "link_back_from": [
        {"source_url": KNOWN[3], "anchor_text": "garden ideas", "reason": "local page"},
        {"source_url": "https://elsewhere.com/", "anchor_text": "x", "reason": "not in sitemap"},
    ],
    "headings": [{"text": "Choosing a Patio", "level": 2}, {"text": "Low Maintenance Options", "level": 9}, {"level": 2}],
    "lists": [{"type": "ul", "items": ["Artificial grass: stays green all year", "Gravel borders"]}, {"type": "ol"}],
}

RETRY_ANSWER = {"internal_links": [{"anchor_text": "Artificial grass", "sentence": "", "target_url": KNOWN[2], "reason": "fixed", "priority": "medium"}]}


class FakeModel:
    """Stands in for Claude: returns canned answers in order and records every prompt."""

    name = "fake-model"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def json(self, prompt):
        self.prompts.append(prompt)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def body(**over):
    b = {
        "client": {"name": "Example Landscapes", "domain": "example-landscapes.co.uk", "notes": "Friendly", "priority_pages": [KNOWN[0]]},
        "post": {"title": "Planning Your Dream Garden", "keyword": "", "url": "", "content": POST},
        "sitemap_urls": KNOWN + ["https://example-landscapes.co.uk/"],
    }
    b.update(over)
    return b


@pytest.fixture(autouse=True)
def no_real_sitemaps(monkeypatch):
    """Tests never reach the internet: finding a client's sitemap fails unless a test fakes it."""
    import app.main as m
    from app.crawl import Blocked
    m._sitemaps.clear()

    async def none(domain, transport=None):
        raise Blocked("no_sitemap")
    monkeypatch.setattr(m, "find_sitemap", none)


@pytest.fixture(autouse=True)
def fast_crawl(monkeypatch):
    """No politeness pauses in tests (pacing has its own tests)."""
    monkeypatch.setenv("POST_DESK_CRAWL_DELAY_S", "0")
    import app.crawl as c
    real_sleep = c.asyncio.sleep
    monkeypatch.setattr(c.asyncio, "sleep", lambda s, *a: real_sleep(0))


@pytest.fixture
def api(monkeypatch):
    """Returns (client, set_model). Each test sets the answers its fake model gives."""
    monkeypatch.setenv("POST_DESK_API_KEY", API_KEY)
    from app.main import get_search
    app.dependency_overrides[get_search] = lambda: None     # no Search Console unless a test sets one up
    holder = {}

    def set_model(*answers):
        holder["model"] = FakeModel(answers)
        app.dependency_overrides[get_model] = lambda: holder["model"]
        return holder["model"]

    client = TestClient(app, headers={"X-API-Key": API_KEY})
    yield client, set_model
    app.dependency_overrides.clear()
