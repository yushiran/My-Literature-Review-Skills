---
name: literature-review
description: Build and query a local literature library for a research topic. Use when the user starts a new project and wants a broad literature review ("帮我做 diffusion 的 literature review", "survey recent work on X"), or later asks what the collected references say about something. Searches OpenAlex / Semantic Scholar / arXiv for recent, high-venue, high-impact work, has a sonnet scout pick the papers, downloads open-access PDFs, converts them with MinerU into Markdown plus images under references/<topic>/, and writes an INDEX.md with every paper's abstract so later questions are answered from the index rather than by walking the library.
---

# Literature review

Two jobs. **Build** a library for a topic, and **answer** questions from a
library that exists. Read [references/workflow.md](references/workflow.md)
once; it is the data contract every script and agent follows.

## Rules that do not bend

1. **Scripts do the deterministic work.** Searching, ranking, downloading,
   converting and indexing are `scripts/*.py`. Do not reimplement any of them
   in a shell loop or by reading pages yourself.
2. **Agents only where a judgement is needed.** `scout` (sonnet) turns the
   topic into research questions and picks the papers; `librarian` (opus)
   writes the reading guide. Nothing else calls a model.
3. **Never traverse the library.** A question about the references starts at
   `references/<topic>/INDEX.md`. Grep it, pick ids, open only those
   `md/<id>/<id>.md`. `INDEX.md` lists the papers that were selected but never
   read; do not cite their content as verified.
4. **MinerU needs a token, and the token is the user's to give.** If
   `convert.py` exits 3, stop and tell the user:
   > MinerU 需要 API token。在 https://mineru.net/apiManage/token 生成一个，然后
   > `mineru-open-api auth`（交互式）或 `export MINERU_TOKEN=...`，我再继续。
   Do not fall back to `flash-extract` on your own: it drops the figures and
   caps at 20 pages. Only if the user says they will not get a token.
5. **Paywalled papers are reachable, and the credentials are the user's to
   give.** Read [references/institutional-access.md](references/institutional-access.md)
   before telling the user a paper cannot be had: it holds the routes, how to find
   the proxy host, the cookie-export procedure to walk the user through, and what
   to do when a session dies. In short: `fetch.py` falls back to Europe PMC (free,
   always on), the Wiley and Elsevier mining APIs, and an institutional EZproxy,
   configured in `~/.config/litrev/access.env` at mode 0600. Never read that file,
   never echo a token, never pass one on a command line, and never invite the user
   to paste a cookie jar into the chat. Verify one paywalled PDF before starting a
   batch. If `fetch.py` exits 2 saying the proxy sent us to its login page, the jar
   is stale: ask for a fresh export, then `fetch.py --retry-no-pdf`.

6. **OpenAlex meters by credit, and the free key raises the ceiling tenfold.**
   One request costs one credit; a paginated query costs several. Measured
   2026-09-13: **1000 requests a day unauthenticated, 10000 with a free key**,
   resetting at midnight UTC. Exhausting it returns `429` with
   `"Insufficient budget"`, and no amount of backing off helps until the reset.
   Building six libraries in a day exhausts the unauthenticated allowance, so
   tell the user once, at the first `429`:
   > OpenAlex 的免费额度用完了(未认证每天 1000 次)。去 https://openalex.org
   > 注册一个免费账号拿 API key,把它写进 `~/.config/litrev/access.env`:
   > `OPENALEX_API_KEY=<key>`,然后 `chmod 600` 那个文件。额度会涨到每天
   > 10000 次,立刻生效,不用等重置。

   The scripts read that file themselves; nothing needs exporting. Never read
   the file back or echo a key, and warn the user that a key pasted into chat
   is in the transcript.

   Semantic Scholar is optional and secondary. Without a key it shares a public
   pool and usually answers 429; `--s2` is opt-in for that reason. A free key
   from https://www.semanticscholar.org/product/api#api-key-form goes in the
   same file as `S2_API_KEY`.

## Build

**There is no target size.** Take every candidate that genuinely answers a
research question, and reject the rest however many that leaves. A library of
fifteen is right when only fifteen are relevant, and one of sixty is right
when sixty are. What must never happen is a flagship paper left out because a
count was already met.

The filter is relevance, not rationing, so it has to be stated as rejection
criteria rather than a quota: keep out papers whose contribution is a domain
application of a method rather than a method you could borrow, and let a high
citation count on one of those count for nothing. Two papers that make the
same point are both worth taking when both are substantive; say in the `why`
how they differ.

`select.py` warns above `--target 30`. That is a warning, not a limit; pass
`--target <the number you actually selected>` to silence it.

### 0. Brief — one confirmation with the user

Ask `scout` for: research questions, queries, `since`, and `seeds` (the
canonical papers the questions presuppose). Show it; wait for a yes.

### 1. Search, seeds, rank — scripts

```sh
uv run scripts/search.py --topic <slug> --since <year> --query "…" … --seed "…" …
uv run scripts/rank.py   --topic <slug> --top 120
```

Seeds enter as `selected`. Read the `saturation:` line: it is the share of
what the round returned that was new, so 5 % or less means the library already
held almost all of it and this query axis is exhausted; go to snowball rather
than adding queries.
arXiv is searched only when OpenAlex returns fewer than 20 hits for a query
(`--arxiv on` to force); Semantic Scholar needs `--s2` and a key.

Venue tiers are in [references/venues.yaml](references/venues.yaml); the user
may edit it.

### 2. Triage — scout, cheap

Give `scout` the brief and `candidates_titles.md`; it writes `triage.json`.

```sh
uv run scripts/rank.py --topic <slug> --only references/<slug>/triage.json
```

It rewrites `candidates.md` only, leaving `candidates_titles.md` as the run
above wrote it, and it filters the whole ranking rather than the `--top` cut,
so a paper the scout kept from below the cut still reaches select. The cut then
applies to what triage kept, silently, so pass the same `--top` as step 1 if the
scout keeps more than 100 papers.

### 3. Select — scout, the costly call, now over 20–40 abstracts not 100

```sh
uv run scripts/select.py --topic <slug> --file references/<slug>/selected.json --triage references/<slug>/triage.json --target <n>
```

If `selected.json` has not appeared 20 minutes after launching the scout,
look for `selected.partial.json`; relaunch and tell it to continue from there.

### 4. Snowball — scripts, then steps 1–3 again on the new candidates only

```sh
uv run scripts/snowball.py --topic <slug> --since <year>
uv run scripts/rank.py --topic <slug>
```

One hop back finds the canon, one hop forward the newest followers. Ranking
here covers the whole library, not just the new papers: `--new-only` needs a
`refreshed` date, and only `pipeline.py --refresh` ever sets one. Repeat
triage and select on `candidates_titles.md`; stop when snowball's printed
`new=` is under 5 % of the `found=` count the last search printed, which is the
whole library. `snowball.py` prints no percentage of its own, and the `back=`
and `forward=` counts beside `new=` are not the base.

### 5. Fetch, convert, index — scripts, in the background

```sh
uv run scripts/pipeline.py --topic <slug> --jobs 4
```

A stale proxy session is detected once at the start and skipped for the run;
the cookie procedure is in references/institutional-access.md. A MinerU
upload timeout switches the remaining upload-only papers to local text
automatically (`--upload-fallback none` to disable). A paper that ended
`failed` keeps that status and its error across runs; to convert it again,
run `convert.py --retry-failed`, which reads the pdf still on disk.

### 6. Reading guide — librarian

```sh
uv run scripts/index.py --topic <slug> --dump-abstracts > /tmp/abstracts.md
```

The dump marks `[unread: …]` and `[text-only]` papers; the guide must too.
Give it to `librarian`, then fold the result in:

```sh
uv run scripts/index.py --topic <slug> --guide references/<slug>/guide.md
```

### 7. Report

Found, selected, converted, unread (paywalled), failed; `INDEX.md` path;
which steps arXiv/S2 skipped; the `found_via` split (query / seed / snowball).

## Refresh — a living review

```sh
uv run scripts/pipeline.py --topic <slug> --refresh
```

Re-runs the stored queries since the last refresh, snowballs forward from
everything past selection, ranks only the new ones, and stops for triage +
select. Triage and select are steps 2 and 3 above, run unchanged: the `--only`
command keeps the new-paper `candidates_titles.md` the refresh just wrote.
Papers found before the last refresh that the scout never triaged stay `found`
and are invisible to `--new-only`, which filters on `found_date`; a plain
`rank.py` run lists them again.
Then `pipeline.py` as usual and `index.py --dump-abstracts --new-only` for the
librarian, giving it the previous `guide.md` so it writes a delta. An empty
dump exits 4 and says so; do not call the librarian on it.

## Answer

When the user asks something about a topic that has a library:

1. Read `references/<topic>/INDEX.md` (the guide first, then the table).
2. Pick the ids whose title or abstract match; usually two to five.
3. Open those `md/<id>/<id>.md`, cite by id and section, and answer.
4. If nothing in the index fits, say so and offer to extend the library
   with a targeted search rather than guessing.

## Layout produced

```
references/<topic>/
  INDEX.md   manifest.json   candidates.md   candidates_titles.md
  triage.json   selected.json   snowball.md   guide.md
  pdf/<id>.pdf
  md/<id>/<id>.md  +  md/<id>/images/
```

Ids are `<year>-<surname>-<slug>`, so a directory listing already sorts by
year.
