# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Search OpenAlex, Semantic Scholar and arXiv and merge the hits into manifest.json.

Every hit becomes a paper in status "found". Existing papers are matched by
DOI, then arXiv id, then normalised title; a match only fills empty fields,
unions sources and keeps the larger citation count. Status is never changed.
Contract: references/workflow.md.
"""
import argparse
import concurrent.futures as cf
import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

MAILTO = "yushiranyushiran@gmail.com"
USER_AGENT = f"literature-review-skill (mailto:{MAILTO})"
TIMEOUT = 60
BACKOFF = (2, 5, 12)
S2_KEY_HINT = (
    "Semantic Scholar rate-limited; skipped. A free key raises the limit: "
    "https://www.semanticscholar.org/product/api#api-key-form (set S2_API_KEY)"
)
OPENALEX_SELECT = ",".join([
    "id", "doi", "ids", "title", "publication_year", "cited_by_count",
    "authorships", "primary_location", "best_oa_location", "locations",
    "abstract_inverted_index", "relevance_score",
])
S2_FIELDS = "title,abstract,year,venue,citationCount,externalIds,openAccessPdf,publicationDate,authors"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
# Used to decide whether an arXiv comment names a real venue.
VENUE_WORDS = re.compile(
    r"\b(NeurIPS|NIPS|ICML|ICLR|CVPR|ICCV|ECCV|WACV|BMVC|AAAI|IJCAI|ACL|EMNLP|NAACL|KDD|"
    r"MICCAI|IPMI|ISBI|ISMRM|MIDL|TMI|TPAMI|TIP|JMLR|SIAM|IEEE|ACM|Nature|Science|"
    r"Transactions|Journal|Proceedings|Conference|Symposium|Workshop|Radiology|NeuroImage|"
    r"Medical Image Analysis|Magnetic Resonance in Medicine|Inverse Problems)\b",
    re.IGNORECASE,
)
# Comment markers meaning "not (yet) published anywhere" -> force venue "arXiv".
NOT_PUBLISHED = re.compile(
    r"\b(submitted to|under review|in submission|preprint|rejected)\b", re.IGNORECASE
)
# A clause that is just a page/figure/table count, not a venue name.
COUNT_CLAUSE = re.compile(r"\b(pages|figures|tables|pp\.)\b", re.IGNORECASE)
# Leading/trailing noise phrases to trim off a venue clause; longer phrases first
# so e.g. "accepted at" matches before the bare "accepted" alternative.
NOISE_PHRASES = (
    "camera-ready", "camera ready", "final version", "extended version", "version",
    "accepted at", "accepted to", "accepted", "to appear in", "to appear at", "in", "at",
)
_NOISE_ALT = "|".join(re.escape(p) for p in NOISE_PHRASES)
NOISE_LEAD = re.compile(rf"^(?:{_NOISE_ALT})\b[:,]?\s*", re.IGNORECASE)
NOISE_TRAIL = re.compile(rf"\s*[:,]?\b(?:{_NOISE_ALT})$", re.IGNORECASE)


def strip_venue_noise(text):
    """Trim NOISE_PHRASES off both ends, repeatedly (they can stack)."""
    prev = None
    while prev != text:
        prev = text
        text = NOISE_TRAIL.sub("", NOISE_LEAD.sub("", text)).strip()
    return text


class SourceDown(Exception):
    """A source did not answer after all retries."""


class RateLimited(Exception):
    """HTTP 429 that survived the backoff schedule."""


# ---------------------------------------------------------------- http

def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def get_json_retry(url, headers=None, name="source"):
    """GET with retries on network errors, 5xx and 429. Raises SourceDown or RateLimited."""
    last = None
    for attempt in range(len(BACKOFF) + 1):
        try:
            return json.loads(http_get(url, headers))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                if attempt == len(BACKOFF):
                    raise RateLimited(url)
            elif e.code < 500:
                # Bad request or auth: retrying will not help.
                raise SourceDown(f"{name} HTTP {e.code} for {url}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = e
        if attempt < len(BACKOFF):
            M.log(f"{name}: {last}; retry in {BACKOFF[attempt]}s")
            time.sleep(BACKOFF[attempt])
    raise SourceDown(f"{name} unreachable: {last}")


# ---------------------------------------------------------------- helpers

def clean(text):
    return " ".join((text or "").split())


def inverted_abstract(inv):
    if not inv:
        return ""
    pos = [(i, w) for w, idxs in inv.items() for i in idxs]
    pos.sort()
    return clean(" ".join(w for _, w in pos))


def arxiv_id_from_url(url):
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+)", url or "", re.IGNORECASE)
    return M.norm_arxiv(m.group(1)) if m else ""


def record(source, **f):
    relevance = f.get("relevance")
    return {
        "source": source,
        "title": clean(f.get("title")),
        "authors": [clean(a) for a in f.get("authors") or [] if clean(a)],
        "year": f.get("year"),
        "venue": clean(f.get("venue")),
        "doi": M.norm_doi(f.get("doi")),
        "arxiv": M.norm_arxiv(f.get("arxiv")) if f.get("arxiv") else "",
        "openalex": f.get("openalex") or "",
        "s2": f.get("s2") or "",
        "citations": int(f.get("citations") or 0),
        "abstract": clean(f.get("abstract")),
        "pdf_url": (f.get("pdf_url") or "").strip(),
        "relevance": float(relevance) if relevance is not None else None,
    }


# ---------------------------------------------------------------- openalex

def openalex_venue(w):
    for loc in [w.get("primary_location")] + (w.get("locations") or []):
        src = (loc or {}).get("source") or {}
        if src.get("display_name"):
            return src["display_name"]
    return ((w.get("host_venue") or {}).get("display_name")) or ""


def openalex_record(w):
    locs = [w.get("primary_location"), w.get("best_oa_location")] + (w.get("locations") or [])
    arxiv = ""
    for loc in locs:
        for key in ("landing_page_url", "pdf_url"):
            arxiv = arxiv_id_from_url((loc or {}).get(key))
            if arxiv:
                break
        if arxiv:
            break
    pdf = ((w.get("best_oa_location") or {}).get("pdf_url")
           or (w.get("primary_location") or {}).get("pdf_url") or "")
    if not pdf:
        # Any other location with a direct pdf, usually an arXiv mirror.
        pdf = next((l.get("pdf_url") for l in locs if l and l.get("pdf_url")), "")
    return record(
        "openalex",
        openalex=(w.get("id") or "").rsplit("/", 1)[-1],
        doi=(w.get("ids") or {}).get("doi") or w.get("doi"),
        title=w.get("title") or w.get("display_name"),
        year=w.get("publication_year"),
        citations=w.get("cited_by_count"),
        authors=[(a.get("author") or {}).get("display_name") or a.get("raw_author_name")
                 for a in w.get("authorships") or []],
        venue=openalex_venue(w),
        pdf_url=pdf,
        abstract=inverted_abstract(w.get("abstract_inverted_index")),
        arxiv=arxiv,
        relevance=w.get("relevance_score"),
    )


def search_openalex(query, since, limit):
    out, cursor = [], "*"
    while len(out) < limit and cursor:
        # title+abstract filter (not the full-text search= param) to keep hits on-topic.
        filt = f"title_and_abstract.search:{query},publication_year:>{since - 1}"
        params = {
            "filter": filt,
            "per-page": min(200, limit - len(out)),
            "cursor": cursor,
            "select": OPENALEX_SELECT,
            "mailto": MAILTO,
        }
        if os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        try:
            # old: data = get_json_retry(url, name="openalex")
            with M.host_gate("api.openalex.org", 0.1):
                data = get_json_retry(url, name="openalex")
        except RateLimited as e:
            raise SourceDown(f"openalex rate-limited: {e}")
        results = data.get("results") or []
        if not results:
            break
        out.extend(openalex_record(w) for w in results)
        cursor = (data.get("meta") or {}).get("next_cursor")
    return out[:limit]


def openalex_by_doi(doi, sel):
    """One exact OpenAlex works/<doi> lookup, gated; None if OpenAlex has no such work."""
    base = "https://api.openalex.org/works"
    with M.host_gate("api.openalex.org", 0.1):
        w = get_json_retry(f"{base}/https://doi.org/{doi}?{urllib.parse.urlencode(sel)}", name="openalex")
    return openalex_record(w) if w.get("id") else None


def openalex_by_title(title, sel):
    """Top-5-by-citations OpenAlex title search; an exact fuzzy-title match wins, else the
    most-cited hit (logged, since it is a guess)."""
    base = "https://api.openalex.org/works"
    filt = {"filter": f"title.search:{title}", "per-page": 5, "sort": "cited_by_count:desc", **sel}
    with M.host_gate("api.openalex.org", 0.1):
        data = get_json_retry(f"{base}?{urllib.parse.urlencode(filt)}", name="openalex")
    hits = [openalex_record(w) for w in data.get("results") or []]
    exact = [h for h in hits if M.fuzzy_title(h["title"]) == M.fuzzy_title(title)]
    if exact:
        return exact[0]
    if hits:
        M.log(f"seed: no exact title match for {title!r}; taking the most cited hit {hits[0]['title']!r}")
        return hits[0]
    return None


def arxiv_title(aid):
    """One paper's title straight from arXiv's own API; '' if arXiv has no such id.
    Raises SourceDown on a network or parse failure, same as search_arxiv."""
    url = f"http://export.arxiv.org/api/query?id_list={aid}&max_results=1"
    try:
        with M.host_gate("export.arxiv.org", 3.0):
            body = http_get(url)
        entry = ET.fromstring(body).find(ATOM + "entry")
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as e:
        raise SourceDown(f"arxiv unreachable: {e}")
    return clean(entry.findtext(ATOM + "title")) if entry is not None else ""


def lookup_seed(spec):
    """Resolve 'doi:…', 'arxiv:…' or a title to one OpenAlex record, or None."""
    spec = spec.strip()
    sel = {"select": OPENALEX_SELECT, "mailto": MAILTO}
    if os.environ.get("OPENALEX_API_KEY"):
        sel["api_key"] = os.environ["OPENALEX_API_KEY"]
    if spec.lower().startswith("doi:"):
        return openalex_by_doi(M.norm_doi(spec[4:]), sel)
    if spec.lower().startswith("arxiv:"):
        aid = M.norm_arxiv(spec[6:])
        # old: filter=locations.landing_page_url:... -- not a documented OpenAlex filter, 4xxs.
        # Chain instead: the arXiv DataCite DOI is an exact match with no extra host; if OpenAlex
        # has not indexed that DOI, fall back to a title search using arXiv's own title for it.
        # old: r = openalex_by_doi(M.norm_doi(f"10.48550/arxiv.{aid}"), sel)
        try:
            # An unindexed DOI is OpenAlex's normal 404, i.e. get_json_retry raises SourceDown
            # here, not an empty result -- that is the common case (most arXiv ids have no
            # 10.48550 DOI in OpenAlex), so treat it as a miss and fall through to arXiv's own
            # title. RateLimited is not a miss and must still propagate.
            r = openalex_by_doi(M.norm_doi(f"10.48550/arxiv.{aid}"), sel)
        except SourceDown:
            r = None
        if r:
            return r
        title = arxiv_title(aid)
        return openalex_by_title(title, sel) if title else None
    return openalex_by_title(spec, sel)


# ---------------------------------------------------------------- semantic scholar

def s2_record(p):
    ext = p.get("externalIds") or {}
    year = p.get("year") or ((p.get("publicationDate") or "")[:4] or None)
    return record(
        "s2",
        s2=p.get("paperId"),
        doi=ext.get("DOI"),
        arxiv=ext.get("ArXiv"),
        title=p.get("title"),
        year=int(year) if year else None,
        venue=p.get("venue"),
        citations=p.get("citationCount"),
        authors=[a.get("name") for a in p.get("authors") or []],
        pdf_url=(p.get("openAccessPdf") or {}).get("url"),
        abstract=p.get("abstract"),
    )


def search_s2(query, since, limit):
    """Returns records. Raises RateLimited or SourceDown; the caller decides to skip."""
    headers = {}
    if os.environ.get("S2_API_KEY"):
        headers["x-api-key"] = os.environ["S2_API_KEY"]
    out, offset = [], 0
    while len(out) < limit:
        params = {
            "query": query,
            "limit": min(100, limit - len(out)),
            "offset": offset,
            "fields": S2_FIELDS,
            "year": f"{since}-",
        }
        url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params)
        data = get_json_retry(url, headers, name="s2")
        batch = data.get("data") or []
        if not batch:
            break
        out.extend(s2_record(p) for p in batch)
        if data.get("next") is None:
            break
        offset = data["next"]
    return out[:limit]


# ---------------------------------------------------------------- arxiv

def arxiv_venue(comment, journal_ref):
    if journal_ref and clean(journal_ref):
        return clean(journal_ref)
    comment = clean(comment)
    if not comment or NOT_PUBLISHED.search(comment) or not VENUE_WORDS.search(comment):
        return "arXiv"
    # Keep the clause that names the venue; skip page/figure-count clauses.
    for part in re.split(r"[;.,()]|\s-\s", comment):
        part = part.strip()
        if not part or COUNT_CLAUSE.search(part) or not VENUE_WORDS.search(part):
            continue
        return strip_venue_noise(part) or "arXiv"
    return "arXiv"


def arxiv_record(entry, since):
    def text(tag, ns=ATOM):
        el = entry.find(ns + tag)
        return el.text if el is not None and el.text else ""

    published = text("published")
    year = int(published[:4]) if published[:4].isdigit() else None
    if year is not None and year < since:
        return None
    aid = M.norm_arxiv(text("id"))
    pdf = ""
    for link in entry.findall(ATOM + "link"):
        if link.get("title") == "pdf" and link.get("href"):
            pdf = link.get("href")
            break
    if not pdf and aid:
        pdf = f"https://arxiv.org/pdf/{aid}"
    return record(
        "arxiv",
        arxiv=aid,
        doi=text("doi", ARXIV_NS),
        title=text("title"),
        year=year,
        venue=arxiv_venue(text("comment", ARXIV_NS), text("journal_ref", ARXIV_NS)),
        authors=[clean(a.findtext(ATOM + "name")) for a in entry.findall(ATOM + "author")],
        pdf_url=pdf,
        abstract=text("summary"),
    )


def search_arxiv(query, since, limit):
    words = [w for w in re.findall(r"[A-Za-z0-9]+", query)]
    if not words:
        return []
    sq = "+AND+".join("all:" + urllib.parse.quote(w) for w in words)
    url = (f"http://export.arxiv.org/api/query?search_query={sq}&start=0"
           f"&max_results={limit}&sortBy=submittedDate&sortOrder=descending")
    last = None
    for attempt in range(len(BACKOFF) + 1):
        try:
            # old: body = http_get(url)
            with M.host_gate("export.arxiv.org", 3.0):
                body = http_get(url)
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < len(BACKOFF):
                M.log(f"arxiv: {e}; retry in {BACKOFF[attempt]}s")
                time.sleep(BACKOFF[attempt])
    else:
        raise SourceDown(f"arxiv unreachable: {last}")
    root = ET.fromstring(body)
    out = []
    for entry in root.findall(ATOM + "entry"):
        rec = arxiv_record(entry, since)
        if rec:
            out.append(rec)
    return out


def arxiv_backfill(manifest, cap=200):
    """Fill missing abstract/pdf_url for papers with an arxiv id, via id_list batches of 50."""
    targets = [p for p in manifest["papers"].values() if not p.get("abstract") and p.get("arxiv")][:cap]
    filled = 0
    for i in range(0, len(targets), 50):
        batch = targets[i:i + 50]
        # old: if i: time.sleep(3)   # replaced by the host_gate below, which also serialises with search_arxiv
        ids = ",".join(p["arxiv"] for p in batch)
        url = f"http://export.arxiv.org/api/query?id_list={ids}&max_results=50"
        try:
            # old: root = ET.fromstring(http_get(url))
            with M.host_gate("export.arxiv.org", 3.0):
                root = ET.fromstring(http_get(url))
        except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as e:
            M.log(f"arxiv backfill: batch failed, {e}")
            continue
        by_id = {}
        for entry in root.findall(ATOM + "entry"):
            rec = arxiv_record(entry, since=0)  # since=0: never drop for year
            if rec:
                by_id[rec["arxiv"]] = rec
        for p in batch:
            rec = by_id.get(p["arxiv"])
            if not rec:
                continue
            if rec["abstract"] and not p.get("abstract"):
                p["abstract"] = rec["abstract"]
                filled += 1
            if rec["pdf_url"] and not p.get("pdf_url"):
                p["pdf_url"] = rec["pdf_url"]
    return filled


# ---------------------------------------------------------------- merge

FILL_FIELDS = ("title", "year", "abstract", "pdf_url", "doi", "arxiv", "s2", "openalex", "venue")


PREPRINT_VENUES = ("arxiv", "biorxiv", "medrxiv", "ssrn", "")


def near_duplicate(p: dict, r: dict) -> bool:
    """Same paper under a slightly different title: fused-hyphen match, or same first
    author + year and >= 80 % shared informative title tokens."""
    if M.fuzzy_title(p.get("title")) == M.fuzzy_title(r["title"]):
        return True
    a, b = M.title_tokens(p.get("title")), M.title_tokens(r["title"])
    if not a or not b:
        return False
    same_author = M.surname((p.get("authors") or [""])[0]) == M.surname((r["authors"] or [""])[0])
    same_year = p.get("year") in (None, r["year"]) or r["year"] is None
    return same_author and same_year and len(a & b) / len(a | b) >= 0.8


# def merge(manifest, recs):                              # old: no provenance
def merge(manifest, recs, via="query"):
    """Adds recs to manifest tagged with how they were found; returns number of new papers."""
    by_doi, by_arxiv, by_title, by_fuzzy, by_openalex = {}, {}, {}, {}, {}

    def index(p):
        if p.get("doi"):
            by_doi.setdefault(M.norm_doi(p["doi"]), p["id"])
        if p.get("arxiv"):
            by_arxiv.setdefault(M.norm_arxiv(p["arxiv"]), p["id"])
        if p.get("openalex"):
            by_openalex.setdefault(p["openalex"], p["id"])
        if p.get("title"):
            by_title.setdefault(M.norm_title(p["title"]), p["id"])
            by_fuzzy.setdefault(M.fuzzy_title(p["title"]), p["id"])

    for p in manifest["papers"].values():
        index(p)

    new = 0
    for r in recs:
        if not r["title"]:
            continue
        pid = (by_doi.get(r["doi"]) if r["doi"] else None) \
            or (by_arxiv.get(r["arxiv"]) if r["arxiv"] else None) \
            or (by_openalex.get(r["openalex"]) if r.get("openalex") else None) \
            or by_title.get(M.norm_title(r["title"])) \
            or by_fuzzy.get(M.fuzzy_title(r["title"]))
        if not pid:
            # Last resort: same first author and year, near-identical token set.
            pid = next((q["id"] for q in manifest["papers"].values() if near_duplicate(q, r)), None)
        if pid:
            p = manifest["papers"][pid]
            for k in FILL_FIELDS:
                if not p.get(k) and r.get(k):
                    p[k] = r[k]
            # A published version outranks the preprint record it matched.
            if (p.get("venue") or "").lower() in PREPRINT_VENUES and (r["venue"] or "").lower() not in PREPRINT_VENUES:
                p["venue"], p["year"] = r["venue"], r["year"] or p.get("year")
                if r["doi"]:
                    p["doi"] = r["doi"]
                M.log(f"merge: {pid}: published version found ({r['venue']})")
            if not p.get("authors") and r["authors"]:
                p["authors"] = r["authors"]
            p["citations"] = max(int(p.get("citations") or 0), r["citations"])
            if r["relevance"] is not None:
                p["relevance"] = r["relevance"] if p.get("relevance") is None else max(p["relevance"], r["relevance"])
            if r["source"] not in p.setdefault("sources", []):
                p["sources"].append(r["source"])
            if via not in p.setdefault("found_via", []):
                p["found_via"].append(via)
            index(p)
            continue
        first = r["authors"][0] if r["authors"] else ""
        pid = M.make_id(manifest, r["year"], first, r["title"])
        fields = {k: v for k, v in r.items() if k != "source"}
        p = M.new_paper(pid, sources=[r["source"]], found_via=[via], **fields)
        manifest["papers"][pid] = p
        index(p)
        new += 1
    return new


# ---------------------------------------------------------------- main

def main():
    parser = M.base_parser("Search OpenAlex, Semantic Scholar and arXiv into manifest.json (status found).")
    # old: parser.add_argument("--query", action="append", required=True, help="search query (repeatable)")
    parser.add_argument("--query", action="append", default=[], help="search query (repeatable)")
    parser.add_argument("--seed", action="append", default=[],
                        help="canonical paper to include regardless of --since: a title, doi:<doi> or arxiv:<id> (repeatable)")
    parser.add_argument("--since", type=int, default=_dt.date.today().year - 2,
                        help="earliest publication year (default: current year - 2)")
    parser.add_argument("--limit", type=int, default=100, help="max hits per query per source (default 100)")
    # old: parser.add_argument("--no-s2", action="store_true", help="skip Semantic Scholar")
    parser.add_argument("--s2", action="store_true", help="also query Semantic Scholar (needs S2_API_KEY to be useful)")
    parser.add_argument("--no-s2", action="store_true", help=argparse.SUPPRESS)   # accepted, no-op
    # old: parser.add_argument("--no-arxiv", action="store_true", help="skip arXiv")
    parser.add_argument("--arxiv", choices=("auto", "on", "off"), default="auto",
                        help="auto: OpenAlex indexes arXiv, so search it only when OpenAlex returns fewer than "
                             "20 hits for a query; abstracts are still backfilled from arXiv (default auto)")
    parser.add_argument("--no-arxiv", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--jobs", type=int, default=4, help="concurrent OpenAlex queries (default 4)")
    try:
        args = parser.parse_args()
    except SystemExit as e:
        sys.exit(M.EXIT_USAGE if e.code else M.EXIT_OK)
    if args.limit < 1 or args.since < 1900:
        parser.print_usage(sys.stderr)
        M.log("search.py: --limit must be >= 1 and --since a year")
        sys.exit(M.EXIT_USAGE)
    if args.no_arxiv:  # old alias: fold into the new tri-state flag
        args.arxiv = "off"

    queries = [q.strip() for q in args.query if q.strip()]
    seeds = [s.strip() for s in args.seed if s.strip()]  # blank-filtered, same as queries
    # old: if not queries and not args.seed:
    if not queries and not seeds:
        M.log("search.py: at least one non-empty --query or --seed is required")
        sys.exit(M.EXIT_USAGE)
    manifest = M.load(args)

    recs = []
    counts = {"openalex": 0, "s2": 0, "arxiv": 0}
    openalex_error = None
    # old: s2_skipped = args.no_s2
    s2_skipped = not args.s2
    s2_hint_shown = False

    # 1. OpenAlex, the primary source: fired for every query up front, args.jobs at a time.
    # old: this used to be step 1 inside the per-query loop below, one request at a time
    oa = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = {pool.submit(search_openalex, q, args.since, args.limit): q for q in queries}
        for fut in cf.as_completed(futs):
            q = futs[fut]
            try:
                oa[q] = fut.result()
            except SourceDown as e:
                openalex_error = str(e)
                oa[q] = []
                M.log(f"openalex: failed for {q!r}, {e}")

    for q in queries:
        oa_hits = oa.get(q, [])
        counts["openalex"] += len(oa_hits)
        recs.extend(oa_hits)
        M.log(f"openalex: {len(oa_hits):4d}  {q!r}")

        # 2. Semantic Scholar, opt-in.
        if not s2_skipped:
            try:
                got = search_s2(q, args.since, args.limit)
                counts["s2"] += len(got)
                recs.extend(got)
                M.log(f"s2:       {len(got):4d}  {q!r}")
            except RateLimited:
                s2_skipped = True
                if not s2_hint_shown:
                    M.log(S2_KEY_HINT)
                    s2_hint_shown = True
            except SourceDown as e:
                s2_skipped = True
                M.log(f"s2: skipped for the rest of the run, {e}")

        # 3. arXiv: "on" always searches; "auto" only when OpenAlex found fewer than 20 hits.
        # old: if not args.no_arxiv:
        if args.arxiv == "on" or (args.arxiv == "auto" and len(oa_hits) < 20):
            # old: the manual "if q is not queries[0]: time.sleep(3)" pause moved into
            # search_arxiv's own host_gate call, which paces across processes too.
            try:
                got = search_arxiv(q, args.since, args.limit)
                counts["arxiv"] += len(got)
                recs.extend(got)
                M.log(f"arxiv:    {len(got):4d}  {q!r}")
            except (SourceDown, ET.ParseError) as e:
                M.log(f"arxiv: skipped for {q!r}, {e}")

    new = merge(manifest, recs)

    # Seeds: canonical papers named in the brief, force-selected regardless of --since.
    seeded = 0
    # old: for spec in args.seed:
    for spec in seeds:
        try:
            r = lookup_seed(spec)
        except (SourceDown, RateLimited) as e:
            M.log(f"seed: {spec!r} lookup failed, {e}")
            continue
        if not r:
            M.log(f"seed: {spec!r} not found on OpenAlex")
            continue
        before = set(manifest["papers"])
        merge(manifest, [r], via="seed")
        pid = next(iter(set(manifest["papers"]) - before), None) or next(
            (p["id"] for p in manifest["papers"].values() if p.get("openalex") == r["openalex"]), None)
        if pid is None:
            # r had no title, so merge() dropped it outright: nothing to promote.
            M.log(f"seed: {spec!r} resolved to a record with no title; skipped")
            continue
        p = manifest["papers"][pid]
        if p["status"] in ("found", "rejected"):
            p["status"], p["why"] = "selected", "seed: named in the brief"
        seeded += 1
        if spec not in manifest.setdefault("seeds", []):
            manifest["seeds"].append(spec)

    # old: filled = arxiv_backfill(manifest) if not args.no_arxiv else 0
    filled = arxiv_backfill(manifest) if args.arxiv != "off" else 0
    M.log(f"arxiv backfill: {filled} abstracts")
    for q in queries:
        if q not in manifest["queries"]:
            manifest["queries"].append(q)
    manifest["since"] = args.since if manifest.get("since") is None else min(manifest["since"], args.since)
    M.save(args, manifest)

    s2_summary = "skipped" if (s2_skipped and counts["s2"] == 0) else str(counts["s2"])
    # old: print(f"found={len(manifest['papers'])} new={new} openalex={counts['openalex']} " f"s2={s2_summary} arxiv={counts['arxiv']}", flush=True)
    print(f"found={len(manifest['papers'])} new={new} openalex={counts['openalex']} "
          f"s2={s2_summary} arxiv={counts['arxiv']} seeds={seeded}", flush=True)

    if openalex_error and counts["openalex"] == 0:
        M.log(f"search.py: OpenAlex never answered ({openalex_error}); results from other sources were saved")
        sys.exit(M.EXIT_NETWORK)
    if openalex_error:
        M.log(f"search.py: OpenAlex failed part-way ({openalex_error}); partial results were saved")
        sys.exit(M.EXIT_NETWORK)
    # old: if not recs:
    if not recs and not seeded:
        M.log("search.py: no source returned anything")
        sys.exit(M.EXIT_NOTHING)
    sys.exit(M.EXIT_OK)


if __name__ == "__main__":
    main()
