"""Turn a shortlist of stories into a written brief using Claude.

Two passes, because they are different jobs at different price points:
  1. Triage with Haiku. Cheap relevance judgement over 40+ headlines.
  2. Brief with Sonnet. Analysis, framing, and the deeper layer.
"""

from __future__ import annotations

import json
import logging
import os
import re

from anthropic import Anthropic

from fetch_news import Story

log = logging.getLogger(__name__)

TRIAGE_MODEL = os.getenv("TRIAGE_MODEL", "claude-haiku-4-5-20251001")
BRIEF_MODEL = os.getenv("BRIEF_MODEL", "claude-sonnet-5")

READER_PROFILE = """You are writing for a senior transformation and AI portfolio
leader inside a global property and casualty insurer. He runs governance over a
large book of AI and digital initiatives and briefs executives daily. He already
knows what a large language model is. He does not need definitions, hype, or
vendor marketing language. He needs to know what changed, whether it changes a
decision he owns, and what he should be ready to answer if an executive asks him
about it in a hallway.

House style rules, follow them exactly:
- Lead with the verdict, then the evidence. Never build up to a conclusion.
- Write at strategic altitude. Name the operating implication, not the feature list.
- Plain sentences. No hyphens and no em dashes anywhere in your prose.
- No filler openers such as "In a significant development".
- If something is vendor noise dressed as news, say so plainly."""

JSON_ONLY = """

Output format: reply with a single raw JSON object and nothing else. No
preamble, no explanation, no markdown code fences. Start with the opening brace
and end with the closing brace."""

TRIAGE_PROMPT = """Below are candidate news items from the last day.

Each item is marked [core] for the insurance beat or [wider] for general AI news.

Select the items that genuinely matter to a senior AI leader inside a global
property and casualty insurer. Prioritise, in this order:
1. A named carrier, reinsurer, broker or MGA doing something concrete with AI
   (deployment, results, spend, partnership, failure, litigation).
2. Regulation and supervisory expectations touching AI in insurance.
3. Core platform and vendor moves that change carrier build versus buy decisions.
4. Capability shifts that plausibly reset what is possible in underwriting,
   claims, or distribution within 12 months.
5. Wider AI news only where it would change how he plans, staffs, buys, or
   governs: frontier model releases, enterprise deployment evidence, AI
   regulation, and the economics of compute and vendors. Tag these "Wider AI".

Never let wider items crowd out the insurance beat. At most 4 of your selections
may be [wider], and only if they clear a higher bar than the core items.

Reject stale items. Feeds sometimes resurface an article from years ago with a
fresh timestamp. If a headline describes a product launch, funding round, or
announcement that reads as old news rather than something from the last few
days, reject it and do not select it.

Reject: recruitment notices, webinars, sponsored posts, generic AI explainers,
consumer gadget news, incremental product updates, and stock movement with no
underlying operational news.

Return JSON only, no preamble:
{"selected": [{"id": <int>, "segment": "<Underwriting|Claims|Distribution|Regulation|Capital|Platform|Talent|Wider AI>", "significance": <1-5>}]}

Select at most 12, ordered by significance descending. If fewer than 12 deserve
selection, return fewer.

CANDIDATES:
%s"""

BRIEF_PROMPT = """Write today's brief from these stories.

Return JSON only, matching this shape exactly:
{
  "verdict": "One or two sentences. The single thing that matters today and why it matters to a P&C carrier. If today is quiet, say that plainly.",
  "movers": [
    {
      "id": <int, matching the story id>,
      "segment": "<value chain segment>",
      "severity": <1-5, how much this should move a carrier's plans>,
      "headline": "A short factual headline in your own words, under 12 words",
      "what_happened": "One sentence of fact.",
      "so_what": "One sentence naming the implication for a P&C carrier's operations, economics, or risk posture.",
      "deeper": "Three to five sentences. The detail behind the story: mechanics, numbers, who is exposed, what is unproven, and how it connects to anything else in today's set.",
      "watch_next": "One short line naming the specific next signal to watch for."
    }
  ],
  "radar": [{"id": <int>, "line": "One sentence, factual, worth knowing but not worth acting on."}],
  "regulatory": [{"id": <int>, "line": "One sentence on what a supervisor said or did and who it binds."}],
  "boardroom_question": "One sharp question this brief should prompt him to ask his own organisation today."
}

Rules:
- Between 3 and 6 movers. Quality over quota. A thin news day should produce a
  short brief, not padding.
- A story tagged "Wider AI" earns a mover slot only if it changes a decision a
  carrier owns. Otherwise put it in radar with one clear line on why it matters.
  The insurance beat always leads the brief.
- Every id must come from the supplied stories. Never invent a story, a number,
  a company, or a quote. If a detail is not in the supplied text, leave it out.
- If the source text is only a headline, keep "deeper" to what can be supported
  and say what is still unknown.
- Do not repeat the same story across movers, radar, and regulatory.
- No hyphens and no em dashes in any prose you write.

STORIES:
%s"""


def _client() -> Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=key)


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Assistant prefill would be tidier, but not every model accepts a
    conversation that ends on an assistant turn, so we ask for bare JSON in
    the prompt and parse defensively here instead.
    """
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def _call(client: Anthropic, model: str, prompt: str, max_tokens: int) -> dict:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=READER_PROFILE + JSON_ONLY,
        messages=[{"role": "user", "content": prompt}],
    )
    body = "".join(block.text for block in response.content
                   if getattr(block, "type", "") == "text")
    log.info("%s used %d in / %d out tokens", model,
             response.usage.input_tokens, response.usage.output_tokens)
    return _extract_json(body)


def render_candidates(stories: list[Story]) -> str:
    lines = []
    for idx, story in enumerate(stories):
        tier = "core" if getattr(story, "tier", "insurance") == "insurance" else "wider"
        lines.append(
            f"[{idx}] [{tier}] {story.title}\n"
            f"    source: {story.source} ({story.region}) | "
            f"{story.published:%Y-%m-%d %H:%M UTC}\n"
            f"    summary: {story.summary[:420] or 'not available'}"
        )
    return "\n".join(lines)


def triage(client: Anthropic, stories: list[Story]) -> list[dict]:
    payload = _call(client, TRIAGE_MODEL,
                    TRIAGE_PROMPT % render_candidates(stories), 1500)
    selected = payload.get("selected", [])
    valid = [s for s in selected
             if isinstance(s.get("id"), int) and 0 <= s["id"] < len(stories)]
    log.info("triage kept %d of %d", len(valid), len(stories))
    return valid


def write_brief(client: Anthropic, stories: list[Story],
                selected: list[dict]) -> dict:
    chosen = [stories[item["id"]] for item in selected]
    lines = []
    for item, story in zip(selected, chosen):
        lines.append(
            f"[{item['id']}] {story.title}\n"
            f"    segment hint: {item.get('segment', 'unclassified')} | "
            f"significance hint: {item.get('significance', 3)}\n"
            f"    source: {story.source} | url: {story.url}\n"
            f"    text: {story.summary[:900] or 'headline only, no body text available'}"
        )
    brief = _call(client, BRIEF_MODEL, BRIEF_PROMPT % "\n\n".join(lines), 4000)
    return attach_sources(brief, stories)


def attach_sources(brief: dict, stories: list[Story]) -> dict:
    """Bind model output back to real URLs so nothing links to a hallucination."""
    def bind(entry: dict) -> dict | None:
        idx = entry.get("id")
        if not isinstance(idx, int) or not 0 <= idx < len(stories):
            return None
        story = stories[idx]
        entry["url"] = story.url
        entry["source"] = story.source
        entry["original_title"] = story.title
        entry["published"] = story.published.isoformat()
        return entry

    for key in ("movers", "radar", "regulatory"):
        brief[key] = [e for e in (bind(x) for x in brief.get(key, []) or []) if e]
    return brief


def fallback_brief(stories: list[Story]) -> dict:
    """Used when the API is unreachable. A raw list beats no email at all."""
    top = stories[:6]
    return {
        "verdict": ("Automated analysis was unavailable for this run, so this is "
                    "the ranked source list without commentary."),
        "degraded": True,
        "movers": [{
            "segment": "Unclassified",
            "severity": 3,
            "headline": s.title[:110],
            "what_happened": s.summary[:220] or "See source.",
            "so_what": "Not assessed on this run.",
            "deeper": s.summary[:600] or "No body text was available in the feed.",
            "watch_next": "",
            "url": s.url,
            "source": s.source,
            "original_title": s.title,
        } for s in top],
        "radar": [{"line": s.title, "url": s.url, "source": s.source}
                  for s in stories[6:16]],
        "regulatory": [],
        "boardroom_question": "",
    }


def build(stories: list[Story]) -> dict:
    if not stories:
        return {"verdict": "No qualifying stories in the last 24 hours.",
                "movers": [], "radar": [], "regulatory": [],
                "boardroom_question": "", "empty": True}
    try:
        client = _client()
        selected = triage(client, stories)
        if not selected:
            selected = [{"id": i, "segment": "Unclassified", "significance": 3}
                        for i in range(min(6, len(stories)))]
        return write_brief(client, stories, selected)
    except Exception as exc:  # noqa: BLE001
        log.error("synthesis failed, falling back to raw list: %s", exc)
        return fallback_brief(stories)
