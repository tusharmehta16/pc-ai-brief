"""Daily AI x P&C insurance brief. Fetch, rank, write, publish, send."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import deliver
import fetch_news
import rank
import render
import synthesize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config")
TEMPLATES = os.path.join(ROOT, "templates")
DOCS = os.path.join(ROOT, "docs")
STATE = os.path.join(ROOT, "state", "seen.json")

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("brief")


def archive_base() -> str:
    base = os.getenv("ARCHIVE_BASE_URL", "").rstrip("/")
    return base or "."


def sample_stories() -> list[fetch_news.Story]:
    """Fixture data so --sample can render a real looking edition offline."""
    from datetime import timezone
    now = datetime.now(timezone.utc)
    raw = [
        ("Chubb expands agentic claims triage across North American commercial lines",
         "Carrier Management", 3,
         "The carrier said an agentic workflow now handles first notice of loss "
         "intake and coverage checks on a subset of commercial property claims, "
         "with adjusters reviewing every recommendation before payment."),
        ("OSFI consults on model risk expectations for generative AI under E-23",
         "Canadian Underwriter", 2,
         "The consultation asks federally regulated insurers how they are "
         "applying existing model risk controls to generative systems used in "
         "underwriting and claims, with responses due in the autumn."),
        ("Lloyd's syndicate reports 11 point cut in quote turnaround using AI submission triage",
         "Insurance Post", 1,
         "The syndicate credits automated submission intake for faster quoting "
         "on delegated business, though it declined to share loss ratio impact."),
        ("Guidewire adds underwriting copilot to its core platform release",
         "Digital Insurance", 4,
         "The vendor bundled a generative assistant into its policy module, "
         "raising build versus buy questions for carriers running custom stacks."),
        ("Insurtech raises 60 million for AI powered commercial property valuation",
         "Coverager", 5,
         "The round was led by an existing investor and values the company at "
         "roughly 400 million on undisclosed revenue."),
        ("NAIC working group signals scrutiny of third party AI model documentation",
         "Insurance Journal", 6,
         "Regulators want carriers to evidence oversight of vendor supplied "
         "models, not just their own."),
    ]
    return [fetch_news.Story(title=t, url=f"https://example.com/story/{i}",
                            source=s, region="Global", source_weight=w,
                            published=now, summary=body, score=40 - i)
            for i, (t, s, w, body) in enumerate(raw)]


def sample_brief(stories: list[fetch_news.Story]) -> dict:
    """A written brief in the exact shape Claude returns, for offline preview."""
    movers = [
        (0, "Claims", 5, "Chubb puts agentic triage into live commercial claims",
         "Chubb moved an agentic first notice of loss workflow into production on "
         "part of its North American commercial property book, with adjuster review "
         "before any payment.",
         "A tier one carrier has now accepted the control model of human review at "
         "the payment gate rather than at every step, which resets what your own "
         "risk function can call unproven.",
         "The scope matters more than the headline. This is intake and coverage "
         "verification, not reserving or settlement authority, and the adjuster "
         "still signs. That places the control at the point of financial impact and "
         "leaves the volume work to the system. The unproven part is cycle time at "
         "scale during a catastrophe surge, when intake volume spikes and the model "
         "sees claim types outside its training distribution. Watch whether the "
         "carrier reports leakage or reopened claim rates rather than speed.",
         "Any disclosure of leakage or reopen rates in the next quarterly call."),
        (1, "Regulation", 4, "OSFI opens consultation on generative AI under E-23",
         "OSFI asked federally regulated insurers how existing model risk controls "
         "are being applied to generative systems in underwriting and claims.",
         "Consultation responses become the evidence base for the next examination "
         "cycle, so what you write now is what you will be held to later.",
         "E-23 was written for models with stable inputs and measurable outputs, and "
         "generative systems fit that frame badly. The likely pressure points are "
         "model inventory completeness, ownership of vendor supplied models, and "
         "what counts as validation when output is not deterministic. Carriers that "
         "already run a single inventory across build and buy will answer this "
         "quickly. Those with shadow tooling in marketing and distribution will find "
         "the inventory question the expensive one.",
         "The consultation closing date and whether peer carriers publish responses."),
        (3, "Platform", 4, "Guidewire bundles an underwriting copilot into core",
         "Guidewire added a generative assistant to its policy module in the latest "
         "core platform release.",
         "Every carrier running a custom underwriting assistant now has to justify "
         "that spend against something arriving in a release note.",
         "Core platform vendors absorbing AI features is the pattern that decides "
         "build versus buy for the next three years. The question is not whether the "
         "bundled copilot is better, because it usually is not at launch. It is "
         "whether your differentiated logic sits somewhere the vendor cannot reach, "
         "and whether your integration cost falls once the capability is inside the "
         "platform you already pay for. Custom builds that only wrap a model in a "
         "chat window are the ones now exposed.",
         "Pricing disclosure, and whether it lands in the base licence or as a module."),
    ]
    return {
        "verdict": ("A tier one carrier moved agentic claims work into production on "
                    "the same day the Canadian supervisor opened a consultation on "
                    "exactly that class of system. The gap between deployment and "
                    "supervisory expectation is now the thing to manage."),
        "movers": [{
            "id": idx, "segment": seg, "severity": sev, "headline": head,
            "what_happened": what, "so_what": sow, "deeper": deep,
            "watch_next": nxt,
            "url": stories[idx].url, "source": stories[idx].source,
            "original_title": stories[idx].title,
        } for idx, seg, sev, head, what, sow, deep, nxt in movers],
        "radar": [
            {"id": 2, "line": "A Lloyd's syndicate credits submission triage with "
                              "faster quoting but will not share loss ratio impact.",
             "url": stories[2].url, "source": stories[2].source},
            {"id": 4, "line": "An AI commercial property valuation startup raised 60 "
                              "million at a reported 400 million valuation.",
             "url": stories[4].url, "source": stories[4].source},
        ],
        "regulatory": [
            {"id": 5, "line": "An NAIC working group signalled it expects carriers to "
                              "evidence oversight of vendor supplied models, not only "
                              "models they build themselves.",
             "url": stories[5].url, "source": stories[5].source},
        ],
        "boardroom_question": ("If a regulator asked today for our complete inventory "
                               "of AI models including vendor supplied ones, how many "
                               "days would it take us to produce it, and who owns it?"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily AI x P&C insurance brief")
    parser.add_argument("--dry-run", action="store_true",
                        help="build everything, send nothing")
    parser.add_argument("--sample", action="store_true",
                        help="use fixture stories, no network fetch")
    parser.add_argument("--no-ai", action="store_true",
                        help="skip Claude, emit the ranked list only")
    parser.add_argument("--window", type=int, default=30,
                        help="hours of news to consider")
    parser.add_argument("--min-score", type=float, default=9.0)
    args = parser.parse_args()

    timezone_name = os.getenv("BRIEF_TZ", "America/Toronto")
    now = datetime.now(ZoneInfo(timezone_name))

    sources = fetch_news.load_sources(os.path.join(CONFIG, "sources.yaml"))
    rules = rank.load_scoring(os.path.join(CONFIG, "scoring.yaml"))
    source_count = len(sources.get("feeds", [])) + len(
        (sources.get("google_news") or {}).get("queries", []))

    if args.sample:
        shortlisted = sample_stories()
    else:
        raw = fetch_news.collect(sources, window_hours=args.window)
        seen = rank.load_seen(STATE)
        shortlisted = rank.shortlist(raw, rules, seen,
                                     min_score=args.min_score)

    if shortlisted and not args.sample:
        # Verify against each publisher's own page before spending model
        # tokens on something an aggregator restamped from 2017.
        shortlisted = fetch_news.enrich(shortlisted)

    if not shortlisted:
        log.warning("nothing qualified today, no email sent")
        return 0

    if args.no_ai:
        brief = synthesize.fallback_brief(shortlisted)
    elif args.sample and not os.getenv("ANTHROPIC_API_KEY"):
        log.info("sample mode without an API key, using the canned brief")
        brief = sample_brief(shortlisted)
    else:
        brief = synthesize.build(shortlisted)

    stats = {"sources": source_count, "qualified": len(shortlisted)}
    archive_file = f"{now:%Y-%m-%d}.html"
    archive_url = f"{archive_base()}/{archive_file}"

    output = render.render_all(brief, stats, now, TEMPLATES, archive_url)
    render.publish_archive(output["archive"], brief, now, DOCS, TEMPLATES)

    preview = os.path.join(ROOT, "preview")
    os.makedirs(preview, exist_ok=True)
    with open(os.path.join(preview, "email.html"), "w", encoding="utf-8") as fh:
        fh.write(output["html"])
    with open(os.path.join(preview, "brief.json"), "w", encoding="utf-8") as fh:
        json.dump(brief, fh, indent=2, default=str)

    log.info("subject: %s", output["subject"])

    if args.dry_run:
        log.info("dry run, nothing sent. preview at preview/email.html")
        return 0

    deliver.send(output["subject"], output["html"], output["text"])

    if not args.sample:
        seen = rank.mark_seen(rank.load_seen(STATE), shortlisted)
        rank.save_seen(STATE, seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
