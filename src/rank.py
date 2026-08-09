"""Score, deduplicate, and shortlist candidate stories."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache

import yaml

from fetch_news import Story

log = logging.getLogger(__name__)

SEEN_TTL_DAYS = 14
NEAR_DUPLICATE = 0.84


def load_scoring(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def broad_scope() -> bool:
    """BRIEF_SCOPE=broad widens the brief beyond insurance to general AI news."""
    return os.getenv("BRIEF_SCOPE", "pc").lower() in ("broad", "all", "wide")


def reg_score_hint(blob: str, rules: dict) -> bool:
    hits, _ = count_hits(blob, rules.get("regulatory_terms", []))
    return hits > 0


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@lru_cache(maxsize=2048)
def term_pattern(term: str) -> re.Pattern:
    """Match a term as a whole word, allowing a trailing plural s.

    Substring matching was the original approach and it was wrong in both
    directions. "ai" matched inside "said", "claim", and "available", while
    "claim" failed to match "claims". Boundaries are defined against letters
    and digits only, so "AI-enabled" and "(AI)" both count as a hit.
    """
    return re.compile(
        r"(?<![a-z0-9])" + re.escape(term) + r"s?(?![a-z0-9])")


def count_hits(haystack: str, terms) -> tuple[float, list[str]]:
    total, hit = 0.0, []
    items = terms.items() if isinstance(terms, dict) else ((t, 1) for t in terms)
    for term, weight in items:
        if term_pattern(term).search(haystack):
            total += float(weight)
            hit.append(term)
    return total, hit


def score_story(story: Story, rules: dict) -> Story:
    blob = f"{story.title} {story.summary}".lower()

    ai_score, ai_hits = count_hits(blob, rules["ai_terms"])
    ins_score, ins_hits = count_hits(blob, rules["insurance_terms"])
    carrier_score, carriers = count_hits(blob, rules.get("carriers", []))
    major_score, majors = count_hits(blob, rules.get("ai_majors", []))

    # Every story needs a real AI signal. "automation" on its own is not one.
    if not ai_hits or ai_score < 2:
        story.score = 0.0
        return story

    # Tier 1, the core beat. A named carrier counts as the insurance side,
    # because headlines often read "Allianz partners with OpenAI" with no
    # generic insurance word in them.
    insurance_story = bool(ins_hits or carriers)

    # Tier 2, the wider AI beat. Only reachable when BRIEF_SCOPE is broad, and
    # only for substantive AI news, not every product blog with "AI" in it.
    wider_story = broad_scope() and ai_score >= 3 and (
        majors or reg_score_hint(blob, rules) or story.source_weight >= 0.9)

    if not insurance_story and not wider_story:
        story.score = 0.0
        return story

    story.tier = "insurance" if insurance_story else "wider"

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

    # A wider AI story is worth knowing but should never outrank the core beat.
    if story.tier == "wider":
        story.score *= 0.85

    story.reasons = [t for t in (carriers + majors + regs + vendors)][:6]
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
    # Top scores every run, qualified or not. Without this a zero tells you
    # nothing about whether the threshold is wrong or the day was just quiet.
    for item in sorted(scored, key=lambda s: s.score, reverse=True)[:8]:
        seen_flag = "" if normalize(item.title)[:90] not in seen else "  [already sent]"
        log.info("  %5.1f  %s%s", item.score, item.title[:76], seen_flag)

    ranked = deduplicate(fresh)

    if broad_scope():
        # Keep the wider AI beat to a minority of the shortlist so the core
        # insurance stories always fill the top of the brief.
        cap = max(3, int(limit * 0.35))
        core = [s for s in ranked if getattr(s, "tier", "insurance") == "insurance"]
        wider = [s for s in ranked if getattr(s, "tier", "insurance") == "wider"][:cap]
        ranked = sorted(core + wider, key=lambda s: s.score, reverse=True)
        log.info("shortlist: %d insurance, %d wider AI", len(core), len(wider))

    return ranked[:limit]


def mark_seen(seen: dict, stories: list[Story]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    for story in stories:
        seen[normalize(story.title)[:90]] = now
    return seen
