"""Shared helpers for the literature-review scripts.

One manifest.json per topic is the single source of truth; every script loads
it, changes only the papers in its input state, and saves atomically. Schema
and state machine: references/workflow.md.
"""
import argparse
import datetime as _dt
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

STATES = ("found", "selected", "rejected", "pdf", "no-pdf", "md", "failed")
EXIT_OK, EXIT_USAGE, EXIT_NETWORK, EXIT_TOKEN, EXIT_NOTHING = 0, 1, 2, 3, 4
UMASK = os.umask(0)  # read once at import; mkstemp files start at 0600
os.umask(UMASK)
STOPWORDS = {
    "a", "an", "the", "of", "for", "and", "or", "in", "on", "to", "with", "via",
    "by", "from", "at", "is", "are", "using", "based", "towards", "toward",
    "into", "its", "as", "we", "our", "new", "novel",
}


def base_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--topic", required=True, help="topic slug, e.g. diffusion")
    p.add_argument("--root", default="references", help="library root (default ./references)")
    return p


def topic_dir(args) -> Path:
    d = Path(args.root) / args.topic
    d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_path(args) -> Path:
    return topic_dir(args) / "manifest.json"


def load(args) -> dict:
    p = manifest_path(args)
    if p.exists():
        return json.loads(p.read_text())
    return {
        "topic": args.topic,
        "created": _dt.date.today().isoformat(),
        "questions": [],
        "queries": [],
        "since": None,
        "papers": {},
    }


def save(args, manifest: dict) -> None:
    """Atomic write so a parallel reader never sees a half-written file."""
    p = manifest_path(args)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".manifest-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    os.chmod(tmp, 0o666 & ~UMASK)  # mkstemp files start at 0600
    os.replace(tmp, p)


def papers_in(manifest: dict, *states: str) -> list:
    return [p for p in manifest["papers"].values() if p.get("status") in states]


def ascii_slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def title_slug(title: str, words: int = 4) -> str:
    toks = [t for t in ascii_slug(title).split() if t not in STOPWORDS and len(t) > 1]
    return "-".join(toks[:words]) or "untitled"


def surname(author: str) -> str:
    """'Hyungjin Chung' -> 'chung'; 'Chung, Hyungjin' -> 'chung'."""
    if not author:
        return "anon"
    if "," in author:
        last = author.split(",")[0]
    else:
        last = author.split()[-1]
    return ascii_slug(last).replace(" ", "") or "anon"


def make_id(manifest: dict, year, first_author: str, title: str) -> str:
    base = f"{year or 'nd'}-{surname(first_author)}-{title_slug(title)}"
    pid, n = base, 1
    while pid in manifest["papers"]:
        n += 1
        pid = f"{base}-{n}"
    return pid


def norm_title(title: str) -> str:
    return " ".join(ascii_slug(title).split())


def norm_doi(doi) -> str:
    if not doi:
        return ""
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi


def norm_arxiv(aid) -> str:
    if not aid:
        return ""
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", aid)
    return m.group(1) if m else aid.strip().lower()


def new_paper(pid: str, **fields) -> dict:
    p = {
        "id": pid, "title": "", "authors": [], "year": None, "venue": "",
        "venue_tier": 3, "doi": "", "arxiv": "", "openalex": "", "s2": "",
        "citations": 0, "abstract": "", "pdf_url": "", "sources": [],
        "relevance": None,  # OpenAlex relevance_score, or null for S2/arXiv-only hits
        "score": 0.0, "status": "found", "why": "",
        "pdf": f"pdf/{pid}.pdf", "md": f"md/{pid}/{pid}.md", "error": "",
    }
    p.update({k: v for k, v in fields.items() if v is not None})
    return p


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
