"""Score, deduplicate, and shortlist candidate stories."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import yaml

from fetch_news import Story

log = logging.getLogger(__name__)

SEEN_TTL_DAYS = 14
NEAR_DUPLICATE = 0.84


def load_scoring(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def count_hits(haystack: str, terms) -> tuple[float, list[str]]:
    total, hit = 0.0, []
    items = terms.items() if isinstance(terms, dict) else ((t, 1) for t in terms)
    for term, weight in items:
        if term in haystack:
            total += float(weight)
            hit.append(term)
    return total, hit


def score_story(story: Story, rules: dict) -> Story:
    blob = f"{story.title} {story.summary}".lower()

    ai_score, ai_hits = count_hits(blob, rules["ai_terms"])
    ins_score, ins_hits = count_hits(blob, rules["insurance_terms"])

    # Hard gate. Both worlds must be present or it is not our story.
    if not ai_hits or not ins_hits:
        story.score = 0.0
        return story

    carrier_score, carriers = count_hits(blob, rules.get("carriers", []))
    vendor_score, vendors = count_hits(blob, rules.get("vendors", []))
    reg_score, regs = count_hits(blob, rules.get("regulatory_terms", []))
    penalty, negatives = count_hits(blob, rules.get("negative_terms", []))

    age_hours = (datetime.now(timezone.utc) - story.published).total_seconds() / 3600
    recency = max(0.0, 6.0 - (age_hours / 6.0))

    story.score = (
        min(ai_score, 9) * 1.0
        + min(ins_score, 12) * 1.0
        + carrier_score * 3.0
        + vendor_score * 1.5
        + reg_score * 2.5
        + story.source_weight * 2.0
        + recency
        - penalty * 4.0
    )

    story.reasons = [t for t in (carriers + regs + vendors)][:6]
    if negatives:
        story.reasons.append(f"downweighted: {negatives[0]}")
    return story


def load_seen(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_TTL_DAYS)
    return {k: v for k, v in data.items()
            if datetime.fromisoformat(v) > cutoff}


def save_seen(path: str, seen: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(seen, fh, indent=1, sort_keys=True)


def deduplicate(stories: list[Story]) -> list[Story]:
    """Collapse the same story reported by six outlets into one entry."""
    kept: list[Story] = []
    for story in sorted(stories, key=lambda s: s.score, reverse=True):
        title_norm = normalize(story.title)
        duplicate = False
        for existing in kept:
            if existing.url == story.url:
                duplicate = True
                break
            ratio = SequenceMatcher(None, title_norm,
                                    normalize(existing.title)).ratio()
            if ratio >= NEAR_DUPLICATE:
                duplicate = True
                break
        if not duplicate:
            kept.append(story)
    return kept


def shortlist(stories: list[Story], rules: dict, seen: dict,
              limit: int = 45, min_score: float = 12.0) -> list[Story]:
    scored = [score_story(s, rules) for s in stories]
    qualified = [s for s in scored if s.score >= min_score]

    fresh = [s for s in qualified if normalize(s.title)[:90] not in seen]
    log.info("scored %d, qualified %d, unseen %d",
             len(scored), len(qualified), len(fresh))

    ranked = deduplicate(fresh)[:limit]
    return ranked


def mark_seen(seen: dict, stories: list[Story]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    for story in stories:
        seen[normalize(story.title)[:90]] = now
    return seen
