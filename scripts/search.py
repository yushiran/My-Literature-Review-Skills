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
            data = get_json_retry(url, name="openalex")
        except RateLimited as e:
            raise SourceDown(f"openalex rate-limited: {e}")
        results = data.get("results") or []
        if not results:
            break
        out.extend(openalex_record(w) for w in results)
        cursor = (data.get("meta") or {}).get("next_cursor")
    return out[:limit]


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
        if i:
            time.sleep(3)
        ids = ",".join(p["arxiv"] for p in batch)
        url = f"http://export.arxiv.org/api/query?id_list={ids}&max_results=50"
        try:
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


def merge(manifest, recs):
    """Adds recs to manifest; returns number of new papers."""
    by_doi, by_arxiv, by_title = {}, {}, {}

    def index(p):
        if p.get("doi"):
            by_doi.setdefault(M.norm_doi(p["doi"]), p["id"])
        if p.get("arxiv"):
            by_arxiv.setdefault(M.norm_arxiv(p["arxiv"]), p["id"])
        if p.get("title"):
            by_title.setdefault(M.norm_title(p["title"]), p["id"])

    for p in manifest["papers"].values():
        index(p)

    new = 0
    for r in recs:
        if not r["title"]:
            continue
        pid = (by_doi.get(r["doi"]) if r["doi"] else None) \
            or (by_arxiv.get(r["arxiv"]) if r["arxiv"] else None) \
            or by_title.get(M.norm_title(r["title"]))
        if pid:
            p = manifest["papers"][pid]
            for k in FILL_FIELDS:
                if not p.get(k) and r.get(k):
                    p[k] = r[k]
            if not p.get("authors") and r["authors"]:
                p["authors"] = r["authors"]
            p["citations"] = max(int(p.get("citations") or 0), r["citations"])
            if r["relevance"] is not None:
                p["relevance"] = r["relevance"] if p.get("relevance") is None else max(p["relevance"], r["relevance"])
            if r["source"] not in p.setdefault("sources", []):
                p["sources"].append(r["source"])
            index(p)
            continue
        first = r["authors"][0] if r["authors"] else ""
        pid = M.make_id(manifest, r["year"], first, r["title"])
        fields = {k: v for k, v in r.items() if k != "source"}
        p = M.new_paper(pid, sources=[r["source"]], **fields)
        manifest["papers"][pid] = p
        index(p)
        new += 1
    return new


# ---------------------------------------------------------------- main

def main():
    parser = M.base_parser("Search OpenAlex, Semantic Scholar and arXiv into manifest.json (status found).")
    parser.add_argument("--query", action="append", required=True, help="search query (repeatable)")
    parser.add_argument("--since", type=int, default=_dt.date.today().year - 2,
                        help="earliest publication year (default: current year - 2)")
    parser.add_argument("--limit", type=int, default=100, help="max hits per query per source (default 100)")
    parser.add_argument("--no-s2", action="store_true", help="skip Semantic Scholar")
    parser.add_argument("--no-arxiv", action="store_true", help="skip arXiv")
    try:
        args = parser.parse_args()
    except SystemExit as e:
        sys.exit(M.EXIT_USAGE if e.code else M.EXIT_OK)
    if args.limit < 1 or args.since < 1900:
        parser.print_usage(sys.stderr)
        M.log("search.py: --limit must be >= 1 and --since a year")
        sys.exit(M.EXIT_USAGE)

    queries = [q.strip() for q in args.query if q.strip()]
    if not queries:
        M.log("search.py: at least one non-empty --query is required")
        sys.exit(M.EXIT_USAGE)
    manifest = M.load(args)

    recs = []
    counts = {"openalex": 0, "s2": 0, "arxiv": 0}
    openalex_error = None
    s2_skipped = args.no_s2
    s2_hint_shown = False

    for q in queries:
        # 1. OpenAlex, the primary source.
        if openalex_error is None:
            try:
                got = search_openalex(q, args.since, args.limit)
                counts["openalex"] += len(got)
                recs.extend(got)
                M.log(f"openalex: {len(got):4d}  {q!r}")
            except SourceDown as e:
                openalex_error = str(e)
                M.log(f"openalex: failed, {e}")

        # 2. Semantic Scholar, optional.
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

        # 3. arXiv, optional, one request per query with a polite pause.
        if not args.no_arxiv:
            if q is not queries[0]:
                time.sleep(3)
            try:
                got = search_arxiv(q, args.since, args.limit)
                counts["arxiv"] += len(got)
                recs.extend(got)
                M.log(f"arxiv:    {len(got):4d}  {q!r}")
            except (SourceDown, ET.ParseError) as e:
                M.log(f"arxiv: skipped for {q!r}, {e}")

    new = merge(manifest, recs)
    filled = arxiv_backfill(manifest) if not args.no_arxiv else 0
    M.log(f"arxiv backfill: {filled} abstracts")
    for q in queries:
        if q not in manifest["queries"]:
            manifest["queries"].append(q)
    manifest["since"] = args.since if manifest.get("since") is None else min(manifest["since"], args.since)
    M.save(args, manifest)

    s2_summary = "skipped" if (s2_skipped and counts["s2"] == 0) else str(counts["s2"])
    print(f"found={len(manifest['papers'])} new={new} openalex={counts['openalex']} "
          f"s2={s2_summary} arxiv={counts['arxiv']}", flush=True)

    if openalex_error and counts["openalex"] == 0:
        M.log(f"search.py: OpenAlex never answered ({openalex_error}); results from other sources were saved")
        sys.exit(M.EXIT_NETWORK)
    if openalex_error:
        M.log(f"search.py: OpenAlex failed part-way ({openalex_error}); partial results were saved")
        sys.exit(M.EXIT_NETWORK)
    if not recs:
        M.log("search.py: no source returned anything")
        sys.exit(M.EXIT_NOTHING)
    sys.exit(M.EXIT_OK)


if __name__ == "__main__":
    main()
