# literature-review

A Claude Code skill that builds a local, indexed literature library for a
research topic and answers later questions from the index.

```
references/<topic>/
  INDEX.md          reading guide + one entry per paper with abstract
  manifest.json     single source of truth for every script
  pdf/<id>.pdf
  md/<id>/<id>.md   MinerU Markdown, images/ beside it
```

Search (OpenAlex, Semantic Scholar, arXiv) → rank by venue tier, citations
and recency → a sonnet scout picks 30 → open-access PDFs are downloaded →
MinerU converts them to Markdown with figures → `INDEX.md` is generated → an
opus librarian writes the reading guide at the top.

Scripts do everything deterministic and cost no tokens; the two agents read
abstracts only.

## Install

```sh
claude plugin marketplace add yushiran/academic-skills
claude plugin install literature-review@yushiran-research
```

`mineru-document-extractor` is installed as a dependency. MinerU's CLI is
`npm install -g mineru-open-api`; conversion with figures needs a token from
https://mineru.net/apiManage/token (`mineru-open-api auth`). Papers with an
arXiv id or an open-access link are converted by URL, fetched by the MinerU
server itself; the local PDF is uploaded only when no URL exists. Semantic Scholar
works without a key but is rate-limited; a free key from
https://www.semanticscholar.org/product/api#api-key-form goes in `S2_API_KEY`.

## Use

> 帮我做 flow matching for MRI reconstruction 的 literature review

Later:

> references 里哪些工作讨论了 posterior sampling 的 step 数？

See [SKILL.md](SKILL.md) for the workflow and
[references/workflow.md](references/workflow.md) for the data contract.
