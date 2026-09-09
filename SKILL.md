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
   `md/<id>/<id>.md`.
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

6. **Semantic Scholar is optional.** Without a key it shares a public pool and
   often answers 429; `search.py` backs off, then skips it and says so.
   OpenAlex alone is enough. Mention the key only when the user asks why S2
   was skipped or asks for more coverage: register at
   https://www.semanticscholar.org/product/api#api-key-form and set
   `S2_API_KEY`.

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

Ask `scout` (model sonnet) for the brief: two or three research questions,
four to eight search queries, and the `since` year (default: two years back).
Show the brief to the user in one message and wait for a yes. This is the only
approval in the build.

### 1. Search and rank — scripts, seconds

```sh
uv run scripts/search.py --topic <slug> --since <year> --query "…" --query "…"
uv run scripts/rank.py   --topic <slug> --top 100
```

`rank.py` writes `candidates.md`: the top 100 by venue tier × citations per
year × recency, each with its abstract. Venue tiers are in
[references/venues.yaml](references/venues.yaml); the user may edit it.

### 2. Select — scout, the one costly call

Give `scout` the brief and `candidates.md`, together with the rejection
criteria for this topic; it returns `selected.json` with as many ids as earn a
place and one line of `why` each. Then:

```sh
uv run scripts/select.py --topic <slug> --file references/<slug>/selected.json --target <n selected>
```

### 3. Fetch, convert, index — scripts, in the background

```sh
uv run scripts/pipeline.py --topic <slug> --jobs 4
```

Run it in the background (`run_in_background`) and keep going. It downloads
the open-access PDFs (paywalled ones become `no-pdf` and keep their abstract in
the index), converts each paper with `mineru-open-api extract` in four parallel
processes, and rebuilds `INDEX.md`. Re-running is safe; it only touches papers
whose state moved.

Conversion is by URL whenever possible: a paper with an arXiv id or an
open-access `pdf_url` is passed to the CLI as that URL, so the MinerU server
fetches it itself and nothing is uploaded from this machine (uploads to the
MinerU OSS bucket time out from many HPC and campus networks, even for a 2 MB
file). The local `pdf/<id>.pdf` is uploaded only as a fallback, or always with
`convert.py --upload`. If `failed` entries show `upload: ... Client.Timeout`,
that is the network, not the token; re-run `convert.py --retry-failed`.

A PDF the user drops into `pdf/<id>.pdf` by hand is picked up on the next run.

A paper obtained through a credential must be uploaded, since MinerU cannot
authenticate, and on some networks that upload never completes — see the
conversion section of [references/institutional-access.md](references/institutional-access.md)
for the measurements. When `failed` entries are all `upload:` errors, look for an
arXiv version by title first, then run `convert.py --local-text-fallback` to read
the rest locally as text.

### 4. Reading guide — librarian, one call

When the pipeline reports nothing left to do:

```sh
uv run scripts/index.py --topic <slug> --dump-abstracts > /tmp/abstracts.md
```

Give that to `librarian` (model opus). It writes `references/<slug>/guide.md`:
a taxonomy of the collected work, the timeline, ten must-reads with one
sentence each, and the gaps. Then fold it in:

```sh
uv run scripts/index.py --topic <slug> --guide references/<slug>/guide.md
```

### 5. Report

Tell the user: how many found, selected, converted, `no-pdf`, `failed`; the
path of `INDEX.md`; and whether S2 was skipped.

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
  INDEX.md   manifest.json   candidates.md   selected.json   guide.md
  pdf/<id>.pdf
  md/<id>/<id>.md  +  md/<id>/images/
```

Ids are `<year>-<surname>-<slug>`, so a directory listing already sorts by
year.
