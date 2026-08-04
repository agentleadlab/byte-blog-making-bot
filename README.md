# RYTE

Agent Lead Lab's copywriter. RYTE learns from copy you've already published,
then writes new SMS, email, ads, scripts and landing pages in that voice — and
turns YouTube videos into scheduled GoHighLevel blog posts.

## Copywriting

Teach it your voice by attaching files:

```
@RYTE learn            ← attach .txt, .md, .csv or .json
@RYTE corpus           ← what it's learned so far
```

Then ask for copy:

```
@RYTE sms about the OTP leads going live Monday
@RYTE email we're raising aged lead prices next month
@RYTE ad for agents stuck at 20 leads a week
@RYTE script hook for a reel on cost per booked appointment
```

RYTE pulls the most relevant past pieces in that format, studies them, and
writes several variants on different angles. Every reply comes with a `.txt`
attachment holding the full text, since Discord truncates long fields.

**Formats:** `sms` `email` `ad` `landing` `script` `social` — plus aliases, so
`texts`, `fb`, `ig`, `vsl`, `reel` and `optin` all resolve. Add one by appending
to `FORMATS` in `src/wilbyte/formats.py`; the schema, the Discord choices, and
the corpus labels all follow from it.

### Feeding it copy

| File | How it's read |
| --- | --- |
| `.txt` / `.md` | One piece, or many separated by a line of `---` |
| `.csv` | Needs a `body`/`copy`/`text`/`message` column. Optional `format`, `title`, `tags` |
| `.json` / `.jsonl` | Objects with a `body` field; optional `format`, `title`, `tags` |

Labels are worked out from the `format` column, the filename (`sms-blasts.csv`),
or the shape of the text — and `@RYTE learn sms` labels a whole upload at once.
Re-uploading the same copy is a no-op: pieces are keyed by a content hash, so
duplicates are skipped.

The library lives at `/data/corpus/pieces.jsonl` — one JSON object per line,
so it greps, diffs, and edits by hand. You can also commit `corpus/*.jsonl` to
the repo and RYTE reads those too.

### The house voice

`prompts/house_voice.md` holds the writing rules — audience, default voice, and
the compliance guardrails (no income claims, no guarantees, no invented numbers
or fake scarcity). The examples override the style guidance on purpose: what the
audience already responds to beats anything written down.

## Blog posts from YouTube

This automates the daily process Wil walks through in
[the Loom](https://www.loom.com/share/1a5392d015334baca9c2ff748667741e): take a
video off the playlist, pull its transcript, run it through the "Copywriting
Frank" Claude project, build a cover image in Canva, then fill in the GHL blog
form and schedule it for 10am on the next open weekday.

Just @ the bot with a playlist link:

```
@RYTE https://youtube.com/playlist?list=PLry8Oc9... 3
```

The bot writes each post, renders the cover, and shows you a review card. Nothing
reaches GHL until you click **Schedule it**.

There's a CLI too, if you'd rather not use Discord:

```
wilbyte run --playlist "https://youtube.com/playlist?list=PLry8Oc9..." -n 3
```

## What it replaces

| Manual step (from the video) | Automated by |
| --- | --- |
| Open each playlist video, click "Show transcript", copy it | `youtube.py` — yt-dlp + youtube-transcript-api |
| Paste transcript + YouTube link into the Copywriting Frank project | `copywriter.py` — Anthropic API, house style in `prompts/copywriting_frank.md` |
| Pick a headline option that isn't the article's H1 | `selection.py` — picks the option least similar to the H1 |
| Combine two headline fragments into a Canva cover image, export, upload | `cover.py` — HTML/CSS template screenshotted at 600×400 |
| Fill slug, category, author, keywords, canonical, description, alt text | `ghl.py` — one API payload; constants live in `config/wilbyte.toml` |
| Find the next weekday with no post, set 10:00 AM, hit Schedule | `scheduler.py` — reads occupied days from GHL, skips weekends |
| Remember which playlist videos are already done | `state.py` — `state/ledger.json` |
| Kicking the whole thing off, and reviewing the result | `bot/` — Discord slash commands with an approve-before-publish card |

The constants that never change per post — category `LeadLab`, author
`Arnold "Tre" Tarpley`, the 13 keywords, the `agentleadlab.com/post/` canonical
prefix, 10am weekdays-only — are all in `config/wilbyte.toml`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium     # skip if Chromium is already provisioned

cp .env.example .env                       # then fill it in
.venv/bin/wilbyte doctor                   # verify config + credentials + GHL access
```

`.env` needs a Discord bot token (see below), an Anthropic API key, and a GHL **Private Integration** token for the
Agent Lead Lab sub-account, created under Settings → Private Integrations with
these scopes:

```
blogs/post.write            blogs/post-update.write     blogs/check-slug.readonly
blogs/category.readonly     blogs/author.readonly       medias.write
medias.readonly
```

`GHL_LOCATION_ID` is in the GHL URL (`/v2/location/<THIS>/...`) and `GHL_BLOG_ID`
is in the blog site URL (`/blogs/site/<THIS>?tab=blog-posts`). `wilbyte doctor`
lists the blog sites it can see if you're unsure which id to use.

## Discord bot

```bash
wilbyte bot
```

### Talking to it

Mention the bot and say what you want — word order doesn't matter.

| Say | It does |
| --- | --- |
| `@RYTE <link> 3` | Writes the next 3 posts and schedules them |
| `@RYTE draft <link>` | Same, but **Save as draft** is the default answer |
| `@RYTE preview <link>` | Builds locally, sends nothing to GHL |
| `@RYTE plan <link>` | Which videos are queued and what day each would land on |
| `@RYTE status` | Posts in the ledger, days booked, next open slots |
| `@RYTE cover Aged, Fresh, Premium \| Why Agents Stall` | Renders a cover image |
| `@RYTE` | Help |

A count is picked up in any position (`3`, `3 posts`, `x3`, `next 3`), and digits
inside the URL are ignored. Add `force` to redo something already in the ledger.

**RYTE only speaks when spoken to.** It replies when a person types `@RYTE` in
the message itself, and stays quiet for everything else — normal conversation,
`@everyone`/`@here`, a role ping that happens to include it, another bot tagging
it, and replies to its own messages with the ping toggle left on (those land in
Discord's mention list without any tag in the text). It's safe to leave in a busy
channel.

The same things work as slash commands, if you prefer a form to fill in:
`/run` `/plan` `/status` `/cover`.

Each review card shows the cover image, title, slug, scheduled slot, description,
and the article H1 — so you can confirm the title genuinely differs from the H1
before approving. Four buttons: **Schedule it**, **Save as draft**, **Skip**,
**Stop the run**. The button you click always wins — starting the run with the
word `draft` only changes what happens on an unattended run, not what
**Schedule it** does.

Skipping leaves the day free, so the next post in the batch takes that slot
instead of leaving a gap. If a publish fails, its slot is returned to the pool
too. Only the person who ran the command can click the buttons, and an
unanswered card times out after an hour (configurable) and is skipped — the
files still land in `out/<slug>/` either way.

Runs are serialized: `/run` refuses to start while another is going, because slot
assignment reads occupied days from GHL and two parallel runs would double-book a
day.

### Setting up the Discord app

1. [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. **Bot** → **Reset Token** → copy it into `DISCORD_BOT_TOKEN`
3. **Installation** → enable the `applications.commands` and `bot` scopes, then use the generated URL to add it to your server
4. Right-click your server → **Copy Server ID** → `DISCORD_GUILD_ID` (without this, new slash commands take up to an hour to appear)

**No privileged intents needed.** Discord delivers real message content for
messages that @mention your bot even without the Message Content intent, which
is the only thing this bot reads. If you've enabled that intent in the portal
anyway and want it requested, set `DISCORD_MESSAGE_CONTENT=true`.

Optionally restrict it with `DISCORD_CHANNEL_IDS` and `DISCORD_ROLE_IDS`; leave
them blank to allow everywhere.

Approval behaviour is under `[discord]` in `config/wilbyte.toml` — set
`require_approval = false` to have `/run` post straight through without the
review step.

## YouTube access

YouTube blocks anonymous scraping from datacenter IPs, so a bot on Railway (or
any cloud host) gets refused — first for transcripts, eventually for video
titles too. The official Data API isn't subject to that, because it
authenticates. It's free, and captions work because you own the channel.

RYTE tries, in order: **Data API → transcript library → yt-dlp → a transcript
you attach by hand**. Any one of them working is enough.

### Setting up the Data API

**Part 1 — the API key** (2 minutes, fixes playlists and video titles)

1. [console.cloud.google.com](https://console.cloud.google.com) → create a project
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**
3. **APIs & Services → Credentials → Create Credentials → API key**
4. Copy it into `YOUTUBE_API_KEY`

**Part 2 — OAuth** (10 minutes, unlocks captions)

Captions can only be downloaded by the video's owner, so this has to be
authorised as the Agent Lead Lab channel.

1. **Credentials → Create Credentials → OAuth client ID** → type **Web application**
2. Under **Authorised redirect URIs** add exactly:
   `https://developers.google.com/oauthplayground`
3. Create it, and copy the **Client ID** and **Client secret**
4. On the **OAuth consent screen**, add your Google account under **Test users**
   (otherwise consent is refused while the app is unpublished)
5. Go to [OAuth Playground](https://developers.google.com/oauthplayground)
6. Click the ⚙ gear, top right → tick **Use your own OAuth credentials** → paste
   the client ID and secret
7. In the left list, find **YouTube Data API v3** and select
   `https://www.googleapis.com/auth/youtube.force-ssl`
8. **Authorize APIs** → sign in **as the account that owns the channel** → allow
9. **Exchange authorization code for tokens** → copy the **Refresh token**

Then set:

```
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...
```

`@RYTE check <link>` reports which route it's using and whether captions came
back human-written or auto-generated.

**Quota:** 10,000 units/day, free. A caption download is 200 units, a playlist
listing 1. Three posts a day is around 600.

### Or send cookies (free, and it works for Studio-made captions)

The Data API has one hard limit worth knowing before you set it up: `captions.list`
will show you a track that `captions.download` then refuses with a 403. Captions
created in YouTube Studio generally can't be downloaded through the API at all,
even by the channel owner — so on a channel that captions its videos the normal
way, the API route lists the track and stops there.

Cookies don't have that problem. "Sign in to confirm you're not a bot" applies to
*anonymous* requests; a signed-in one gets through, and yt-dlp reads the same
caption track the browser does.

1. Install the **Get cookies.txt LOCALLY** extension (Chrome or Firefox)
2. Open youtube.com **logged in**, click the extension, **Export**
3. Open the downloaded `cookies.txt`, copy all of it
4. Paste it into `YOUTUBE_COOKIES`

The account does **not** have to own the channel — any logged-in YouTube session
works, so use a throwaway account rather than the one your channel lives on.

Cookies expire eventually (months, usually). When transcripts start failing again,
re-export and paste. `@RYTE check <link>` says `YouTube cookies loaded` when they
are in use.

### Or just run it on your own machine

A home internet connection isn't blocked, so YouTube works normally with nothing
to configure. See [Running locally](#running-locally) below.

### Or attach the transcript

Paste it out of YouTube into a `.txt` and attach it with the link. No API, no
account, always works — this is the current manual process, minus everything
after it.

### Or a residential proxy

`WEBSHARE_PROXY_USERNAME` / `WEBSHARE_PROXY_PASSWORD`, or `YOUTUBE_PROXY_URL`.
Works, but costs money.

## Running locally

The simplest way around the YouTube block: run RYTE on your own computer, where
the IP looks like a person rather than a datacenter.

```bash
git clone https://github.com/agentleadlab/byte-blog-making-bot.git
cd byte-blog-making-bot
bash scripts/setup.sh        # one time
```

Setup finds a suitable Python, builds a virtualenv, installs everything, fetches
Chromium for the cover images, and creates `.env`. Paste your keys into that
file — the same four values as Railway — and then, whenever you want to work:

```bash
bash scripts/start.sh
```

RYTE comes online in Discord. Leave the window open; `Ctrl-C` when you're done.

**It doesn't need to stay running.** RYTE creates posts in GHL with a scheduled
date, and *GHL* publishes them — so once a batch is approved, your computer is
irrelevant. A typical session is: start it, `@RYTE <playlist> 5`, approve five
posts, quit. They go out at 10am on their days regardless.

**Don't run local and Railway at the same time.** Discord allows one connection
per bot token, so the two will fight over messages. Either pause the Railway
service, or make a second Discord application and give the local copy its own
`DISCORD_BOT_TOKEN`.

Setup is safe to re-run — it skips whatever is already done, which also makes it
the way to update after a `git pull`.

## Deploying to Railway

The repo ships a `Dockerfile` and `railway.json`, so Railway builds it directly.
Without them Railway's autodetect finds a Python project, can't guess a start
command, and the build fails with *"No start command detected"* — a Discord bot
is a worker, not a web app, so there's nothing for it to infer.

The image is based on Microsoft's Playwright image, which already contains
Chromium and its system libraries — that's what renders the cover images.

**1. Point the service at this repo and branch.** Railway picks up
`railway.json` automatically; no build or start command needs setting in the UI.

**2. Add a volume — this one matters.** Railway's filesystem is ephemeral, so
without a volume the ledger resets on every redeploy and the bot reposts videos
it has already done. In the service: **Settings → Volumes → mount at `/data`**.
The image already writes the ledger to `/data/state` and rendered posts to
`/data/out`.

**3. Set the variables** (Settings → Variables):

```
DISCORD_BOT_TOKEN     DISCORD_GUILD_ID      ANTHROPIC_API_KEY
GHL_API_TOKEN         GHL_LOCATION_ID       GHL_BLOG_ID
```

Everything else has a default. There's no `PORT` and no healthcheck — it's a
worker, so Railway showing no public domain is correct.

Deploy logs should end with `Connected as RYTE`. If it restart-loops,
check the deploy logs: a bad `DISCORD_BOT_TOKEN` surfaces as a login failure,
and missing config surfaces as a named `Missing required environment
variable(s)` error.

Any other host works the same way — it's a plain container with one long-running
process. `docker build -t wilbyte . && docker run --env-file .env -v wilbyte:/data wilbyte`
runs it locally.

## CLI usage

```bash
# What would get posted, and on what days — makes no changes.
wilbyte plan --playlist <url>

# Build 3 posts and schedule them. Skips anything already in the ledger.
wilbyte run --playlist <url> -n 3

# Build everything, print the exact API payload, send nothing.
wilbyte run --playlist <url> -n 1 --dry-run

# Create posts as DRAFT instead of SCHEDULED, to review inside GHL first.
wilbyte run --playlist <url> -n 3 --draft

# One video, with a transcript you pasted into a file yourself.
wilbyte run --video <url> --transcript ./transcript.txt

# Write files locally and never touch GHL.
wilbyte run --playlist <url> -n 1 --local-only

# Re-render a cover image on its own.
wilbyte cover --kicker "Aged, Fresh, Premium" --headline "Why Most Agents Never Move Up a Lead Tier"
```

Every run writes a reviewable folder per post under `out/<url-slug>/`:

```
out/insurance-lead-progression-roadmap/
├── post.html        the article body
├── cover.png        600×400 cover image
├── copy.json        raw copywriter output, including all headline options
└── ghl-fields.txt   every field, labelled, for pasting by hand if needed
```

So `--local-only` or `--dry-run` still gives you everything you need to finish a
post manually — the automation degrades to the current process rather than
failing closed.

## The two judgment calls

**Headline.** Wil never uses the copy's own H1 as the blog title: *"I use the
headline that is the opposite or not similar to the headline 1 clearly."*
`choose_title` scores each option's word overlap against the article H1 and takes
the least similar one. If every option overlaps the H1 (≥0.6 Jaccard), it still
picks one but prints a `!` warning so you know to look.

**Cover image.** Two lines: a 3–5 word highlighted kicker, and a bigger headline
underneath. `plan_cover` sources them from *different* headline options and
rejects any kicker that just restates the opening of the article H1 — so the
cover never says the same thing twice.

Both rules are covered in `tests/test_selection.py` against the exact example
from the video.

## Scheduling

One post per weekday at 10:00 AM Eastern (`America/New_York`, so it stays 10:00
through the daylight-saving switch). Before assigning slots the bot reads every
existing post on the blog — published, scheduled and draft alike — and marks
those days occupied, so if you're already booked through the 11th the next post
lands on the 12th. Weekends are skipped, and a slot less than 20 minutes away is
passed over because GHL rejects it.

Change any of this in `config/wilbyte.toml` under `[schedule]`.

## Brand assets

The cover template renders without any assets, using a text wordmark and a
system sans. To match the Canva original exactly, drop files into `assets/` —
see [`assets/README.md`](assets/README.md). No code changes needed.

## Adjusting the copy

`prompts/copywriting_frank.md` is the system prompt. It's a **reconstruction** of
the Organic Post AI Copywriter project instructions, rebuilt from the output
shape visible in the video (headline options with char counts, meta title and
description, URL slug, keyword map, internal link flag). If you can export the
real project instructions, paste them over the body of that file — nothing in the
code reads anything but the file.

The structured fields the pipeline needs (`article_h1`, `headline_options`,
`meta_title`, `meta_description`, `url_slug`, …) are enforced by the tool schema
in `copywriter.py`, not by the prompt, so rewriting the prompt won't break
parsing.

## Verify before the first real run

Two things are built from documentation rather than observed traffic, so confirm
them once:

1. **GHL field names.** The create-post body keys are collected in
   `POST_FIELDS` in `ghl.py`. Run `wilbyte run --video <url> --dry-run` to print
   the exact payload, and compare it against the
   [Create Blog Post docs](https://marketplace.gohighlevel.com/docs/ghl/blogs/create-blog-post/).
   If a key is wrong, fix it in that one dict.
2. **Media upload response.** `upload_media` reads `url` / `fileUrl` from the
   `/medias/upload-file` response. If your response nests it differently, the
   error message prints the whole body.

Start with `--draft` for the first batch, confirm the posts look right in GHL,
then switch to the default scheduled mode.

## Tests

```bash
.venv/bin/python -m pytest
```

105 tests, no network or credentials required. The cover tests render real PNGs
through Chromium, and the bot tests build real embeds and parse real mention
text without a gateway connection.
