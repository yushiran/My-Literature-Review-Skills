# literature-review

<p align="center"><img src="docs/mascot.png" alt="literature-review mascot" width="240"></p>

<p align="center">
  <img src="https://img.shields.io/badge/version-0.2.0-blue" alt="version 0.2.0">
  <img src="https://img.shields.io/badge/Claude%20Code-plugin-8A4FFF" alt="Claude Code plugin">
  <img src="https://img.shields.io/badge/tests-113%20passing-brightgreen" alt="113 tests passing">
  <img src="https://img.shields.io/badge/python-%E2%89%A5%203.9-3776AB" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/dependencies-stdlib%20only-lightgrey" alt="standard library only">
</p>

<p align="center">
  <b>Build a local, indexed literature library for a research topic — then answer later questions from the index instead of searching again.</b>
</p>

---

Ask for a review in plain language. The skill searches the literature, has a
cheap agent triage titles before an expensive one reads abstracts, follows the
citation graph one hop to pick up the canon behind the field and the newest work
citing it, downloads what is open access, converts it to Markdown with figures,
and writes a reading guide at the top of an index. Weeks later you ask a
question and it is answered from that index, not from a fresh search.

```
references/<topic>/
  INDEX.md          reading guide + one entry per paper with abstract
  manifest.json     single source of truth for every script
  guide.md          the librarian's taxonomy, timeline, must-reads, gaps
  candidates.md     what the scout reads when it selects
  pdf/<id>.pdf
  md/<id>/<id>.md   MinerU Markdown, images/ beside it
```

## How it works

| | step | who |
| --- | --- | --- |
| 1 | **Brief** — research questions, queries, a `since` year, and the canonical papers those questions presuppose | scout |
| 2 | **Search + seeds** — OpenAlex first; a seed enters whatever `since` says, so a 2014 classic is not lost to a recency filter | script |
| 3 | **Rank** — venue tier × recency-or-citations × relevance, with a bonus for papers the citation graph reaches twice | script |
| 4 | **Triage** — titles and thirty words each: keep, drop, undecided | scout, cheap |
| 5 | **Select** — full abstracts, but only for what survived triage | scout, costly |
| 6 | **Snowball** — one hop back for the canon, one hop forward for the newest work citing it | script |
| 7 | **Fetch + convert** — open-access PDFs, then MinerU to Markdown with figures | script |
| 8 | **Index + guide** — `INDEX.md`, then a taxonomy, a timeline, the must-reads and the gaps | librarian |

Afterwards, whenever you like, `pipeline.py --refresh` re-runs the stored
queries since the last refresh, snowballs forward, and shows you only what is
new.

## Why it is built this way

**Scripts are deterministic and free; agents are expensive and read abstracts
only.** Everything decidable by a rule — deduplication, scoring, downloading,
conversion, indexing — is a script that costs no tokens. Neither agent ever sees
a full paper.

**Triage before abstracts.** The titles file is about a fifth the size of the
abstracts file, so judging on titles first and paying for abstracts only on the
survivors roughly halves the expensive pass.

**It tells you when to stop.** Every search round prints what fraction of its
return was new; under five per cent means the library already held almost all of
it and that query axis is exhausted. Snowball reports the same shape for the
citation graph, so you stop adding queries on evidence rather than on a hunch.

**It survives being run in parallel.** Requests to one host are serialised
across processes with a file lock, so concurrent searches do not trigger 429s,
and the manifest merges on save instead of overwriting — two scripts writing at
once keep each other's work.

**Credentials live in one file.** `~/.config/litrev/access.env`, read by the
scripts themselves. Nothing needs exporting, and a key never reaches a log line
or an exception message.

**What was never read is marked as such.** A paywalled paper keeps its abstract
in the index and is listed under *Selected but unread*, so nothing it claims can
be quoted as though it had been verified.

## Install

```sh
claude plugin marketplace add yushiran/academic-skills
claude plugin install literature-review@yushiran-research
```

`mineru-document-extractor` comes as a dependency. MinerU's CLI is
`npm install -g mineru-open-api`; conversion with figures needs a token from
<https://mineru.net/apiManage/token> (`mineru-open-api auth`). A paper with an
arXiv id or an open-access link is converted by URL, fetched by the MinerU
server itself; the local PDF is uploaded only when no URL exists.

### Keys

OpenAlex allows **1000 requests a day unauthenticated and 10000 with a free
key** — the difference between one library a day and a tool you can keep using.
Get one at <https://openalex.org>, then:

```sh
mkdir -p ~/.config/litrev
echo 'OPENALEX_API_KEY=<your key>' >> ~/.config/litrev/access.env
chmod 600 ~/.config/litrev/access.env
```

The scripts read that file themselves. Semantic Scholar is off unless `--s2` is
passed; its free key from
<https://www.semanticscholar.org/product/api#api-key-form> goes in the same file
as `S2_API_KEY`. For paywalled papers,
[references/institutional-access.md](references/institutional-access.md) covers
the Wiley and Elsevier mining APIs and an institutional EZproxy session.

## Use

> 帮我做 flow matching for MRI reconstruction 的 literature review

Later, without searching again:

> references 里哪些工作讨论了 posterior sampling 的 step 数？

## Scripts

Each runs standalone with `--topic <slug>`, takes `--help`, and is a step you
can re-run on its own.

| script | does |
| --- | --- |
| `search.py` | OpenAlex / Semantic Scholar / arXiv into the manifest; seeds; the saturation line |
| `rank.py` | scores everything, writes `candidates_titles.md` and `candidates.md` |
| `select.py` | applies the scout's decisions and rejects what triage dropped |
| `snowball.py` | one hop on the citation graph, backward and forward |
| `fetch.py` | downloads open-access PDFs; extra routes when there is none |
| `access.py` | Europe PMC, publisher mining APIs, institutional proxy |
| `convert.py` | MinerU to Markdown, with a local text-only fallback |
| `index.py` | writes `INDEX.md` and the abstract dump for the librarian |
| `pipeline.py` | fetch → convert → index, and `--refresh` for a living review |
| `manifest.py` | the shared state: schema, ids, locking, the merge on save |

## Documentation

- [SKILL.md](SKILL.md) — the workflow the agent follows, step by step
- [references/workflow.md](references/workflow.md) — the data contract: schema, state machine, every flag, every exit code
- [references/institutional-access.md](references/institutional-access.md) — reaching paywalled papers
- [agents/scout.md](agents/scout.md) · [agents/librarian.md](agents/librarian.md) — what each agent is asked to do
