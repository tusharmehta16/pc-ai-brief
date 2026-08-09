# Daily AI x P&C Insurance Brief

An email that lands on weekday mornings with the AI news that actually touches
property and casualty insurers worldwide. Three layers, so you spend thirty
seconds or thirty minutes depending on the day.

1. **The verdict.** One or two sentences at the top. The single thing that matters.
2. **Movers.** Three to six stories, each with what happened, one line of so what,
   a value chain segment tag, and a severity rating out of five.
3. **Deeper.** Every mover links to a full edition page with the analysis behind
   it, plus an "Ask Claude" link that opens a chat pre loaded with the story and
   the right questions for a carrier operator.

## How it works

```
RSS feeds + Google News queries      27 sources by default
        │
        ▼
keyword gate and scoring             AI term AND insurance term required
        │                            carrier names, regulators, vendors weighted up
        ▼
dedupe + 14 day memory               the same story from six outlets becomes one
        │
        ▼
triage pass (Claude Haiku)           cheap relevance judgement over ~45 headlines
        │
        ▼
brief pass (Claude Sonnet)           verdict, movers, deeper layer, radar
        │
        ├──► HTML email               SMTP or Resend
        └──► archive page             GitHub Pages, permanent link per day
```

Cost is roughly two to five cents a day at current pricing, because the
expensive model only ever sees the twelve stories that survived triage.

## Setup

**1. Create the repo**

Push this folder to a new private GitHub repo.

**2. Set repository secrets**

Settings, then Secrets and variables, then Actions, then New repository secret.

| Secret | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | From console.anthropic.com |
| `EMAIL_TO` | The distribution list. Commas, semicolons, or newlines. |
| `EMAIL_REPLY_TO` | Where replies and unsubscribe requests go |
| `EMAIL_FROM` | The sending address |
| `SMTP_USER` | Your Gmail address |
| `SMTP_PASSWORD` | A Gmail app password, not your account password |

For Gmail you need 2FA turned on, then generate an app password at
myaccount.google.com/apppasswords. Paste it with the spaces, it works either way.

If you would rather not use Gmail, set the repository variable `EMAIL_BACKEND`
to `resend`, add a `RESEND_API_KEY` secret, and verify a domain with Resend.
Deliverability is better and the free tier covers a daily send.

**3. Set repository variables**

Same screen, Variables tab.

| Variable | Value |
| --- | --- |
| `ARCHIVE_BASE_URL` | `https://YOURNAME.github.io/REPONAME` |
| `BRIEF_TZ` | `America/Toronto` |
| `EMAIL_MODE` | `bcc`, `to`, or `individual`. Defaults to `bcc`. |

### Sending to more than one person

Put every address in the single `EMAIL_TO` secret, separated however you like.
Then pick a mode:

- `bcc` is the default. One send, and no recipient sees who else is on the list.
- `to` puts everyone in the To line, so the group can see each other and reply all.
  Use it only for a small team that already knows it is a shared list.
- `individual` sends one message per person. Slower, but a single bad address
  cannot bounce the whole send, and it is the friendliest for larger lists.

Adding or removing someone means editing the `EMAIL_TO` secret. There is no
self serve signup, which is the right tradeoff at this size.

Every message carries a `List-Unsubscribe` header and a reply address, so anyone
can get off the list without asking you twice. If you are sending to colleagues
or anyone outside your household, keep that in place.

**4. Turn on Pages**

Settings, then Pages, then Deploy from a branch, branch `main`, folder `/docs`.
That publishes the full editions that the "Go deeper" links point at.

**5. Test it**

Actions tab, Daily P&C AI brief, Run workflow, tick Build without sending.
Check the log, then run it again without the tick to get the email.

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env          # fill it in
set -a && source .env && set +a

python src/main.py --sample --dry-run   # canned content, no network, no API key
python src/main.py --dry-run            # real news, real analysis, no send
python src/main.py                      # the whole thing
```

`--dry-run` writes `preview/email.html`. Open it in a browser to see exactly what
would arrive.

## Tuning it

**Change what it watches.** `config/sources.yaml`. Add a feed with a name, url,
weight, and region. Add a Google News query to reach outlets with no feed. A
higher weight pushes a source up the ranking.

**Change what counts as relevant.** `config/scoring.yaml`. The gate is strict: a
story needs at least one AI term and one insurance term or it never reaches the
model. Add carrier names you care about to `carriers`, they are weighted heavily.

**Change the voice.** `READER_PROFILE` in `src/synthesize.py`. That block is what
makes the brief lead with the verdict instead of narrating. The prompt currently
bans hyphens and em dashes, keep or drop that as you like.

**Change the schedule.** The cron in `.github/workflows/daily-brief.yml` is UTC
and does not follow daylight saving. `5 11 * * 1-5` arrives at 07:05 Toronto in
summer and 06:05 in winter. Shift to `5 12` if you want 07:05 year round in
winter months.

**Fewer or more stories.** `--min-score` raises the bar for what qualifies. The
mover count is set in the prompt in `synthesize.py`, currently three to six with
an explicit instruction not to pad a thin news day.

## Things worth knowing

- Feeds die. A source that fails is logged and skipped, and the run continues.
  Check the Actions log occasionally for a source that has been failing for weeks.
- The dedupe memory lives in `state/seen.json` and is committed back by the
  workflow, so a story that ran Tuesday will not reappear Wednesday.
- If the Anthropic API is unreachable, you still get an email, just a ranked list
  with no analysis and a note saying the run was degraded.
- The model is instructed never to invent a figure, and every link is bound back
  to the real source URL after generation rather than being written by the model.
  Still verify any number before it goes in a deck.
- `docs/` ships with one sample edition so Pages has something to serve. Delete
  it and reset `docs/manifest.json` to `[]` when you want a clean archive.
