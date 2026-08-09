"""Render the brief into an email, an archive page, and an index."""

from __future__ import annotations

import json
import os
import urllib.parse
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

ASK_TEMPLATE = (
    "This came up in my daily AI and P&C insurance brief:\n\n"
    "\"{headline}\"\n{source} | {url}\n\n"
    "Give me the full picture. What actually happened, what it changes for a "
    "global property and casualty carrier across underwriting, claims and "
    "distribution, who is exposed, what is still unproven, and the two questions "
    "I should put to my own team about it this week. Search for the latest if you can."
)


def environment(template_dir: str) -> Environment:
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env


def ask_claude_link(item: dict) -> str:
    prompt = ASK_TEMPLATE.format(
        headline=item.get("headline") or item.get("original_title", ""),
        source=item.get("source", "unknown source"),
        url=item.get("url", ""),
    )
    return "https://claude.ai/new?q=" + urllib.parse.quote(prompt, safe="")


def decorate(brief: dict, archive_url: str) -> dict:
    for index, item in enumerate(brief.get("movers", []), start=1):
        item["deep_link"] = f"{archive_url}#item-{index}"
        item["ask_link"] = ask_claude_link(item)
    for key in ("radar", "regulatory"):
        for item in brief.get(key, []):
            item.setdefault("source", "source")
    return brief


def context(brief: dict, stats: dict, archive_url: str, now: datetime) -> dict:
    lead = brief.get("movers", [{}])[0].get("headline") if brief.get("movers") else None
    subject = f"P&C AI Brief · {now:%a %d %b} · {lead or 'quiet day'}"
    return {
        "brief": brief,
        "stats": stats,
        "archive_url": archive_url,
        "date_long": f"{now:%A, %d %B %Y}",
        "date_iso": f"{now:%Y-%m-%d}",
        "generated_at": f"{now:%H:%M %Z}".strip(),
        "subject": subject[:120],
    }


def render_all(brief: dict, stats: dict, now: datetime, template_dir: str,
               archive_url: str) -> dict:
    env = environment(template_dir)
    brief = decorate(brief, archive_url)
    ctx = context(brief, stats, archive_url, now)
    return {
        "subject": ctx["subject"],
        "html": env.get_template("email.html.j2").render(**ctx),
        "text": plain_text(ctx),
        "archive": env.get_template("archive.html.j2").render(**ctx),
        "context": ctx,
    }


def plain_text(ctx: dict) -> str:
    brief = ctx["brief"]
    out = [f"AI & P&C INSURANCE BRIEF — {ctx['date_long']}", "=" * 52, "",
           "THE VERDICT", brief.get("verdict", ""), ""]
    for index, item in enumerate(brief.get("movers", []), start=1):
        out += [
            f"{index}. [{item.get('segment', 'Signal')} | severity "
            f"{item.get('severity', 3)}/5] {item.get('headline', '')}",
            f"   {item.get('what_happened', '')}",
            f"   So what: {item.get('so_what', '')}",
            f"   Deeper: {ctx['archive_url']}#item-{index}",
            f"   Source: {item.get('url', '')}",
            "",
        ]
    if brief.get("regulatory"):
        out += ["SUPERVISORY WATCH"] + [
            f" - {i.get('line', '')} {i.get('url', '')}" for i in brief["regulatory"]] + [""]
    if brief.get("radar"):
        out += ["ON THE RADAR"] + [
            f" - {i.get('line', '')} {i.get('url', '')}" for i in brief["radar"]] + [""]
    if brief.get("boardroom_question"):
        out += ["ASK YOUR OWN SHOP TODAY", brief["boardroom_question"], ""]
    out += [f"Full edition: {ctx['archive_url']}"]
    return "\n".join(out)


def publish_archive(html: str, brief: dict, now: datetime, docs_dir: str,
                    template_dir: str, keep: int = 120) -> str:
    """Write today's edition and rebuild the index from a small manifest."""
    os.makedirs(docs_dir, exist_ok=True)
    filename = f"{now:%Y-%m-%d}.html"
    with open(os.path.join(docs_dir, filename), "w", encoding="utf-8") as fh:
        fh.write(html)

    manifest_path = os.path.join(docs_dir, "manifest.json")
    editions = []
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                editions = json.load(fh)
        except (json.JSONDecodeError, OSError):
            editions = []

    editions = [e for e in editions if e.get("file") != filename]
    editions.insert(0, {
        "file": filename,
        "date_iso": f"{now:%Y-%m-%d}",
        "date_long": f"{now:%A, %d %B %Y}",
        "verdict": (brief.get("verdict") or "")[:240],
    })
    editions = editions[:keep]

    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(editions, fh, indent=1)

    env = environment(template_dir)
    index_html = env.get_template("index.html.j2").render(editions=editions)
    with open(os.path.join(docs_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(index_html)

    with open(os.path.join(docs_dir, ".nojekyll"), "w", encoding="utf-8") as fh:
        fh.write("")

    return filename
