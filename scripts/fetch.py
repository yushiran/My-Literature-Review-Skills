# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Download the open-access PDF of every `selected` paper.

Candidates are tried in order and the first success wins: pdf_url, then
arXiv /pdf/<id>, then the OpenAlex best_oa_location for the DOI (one extra
lookup, only when the first two gave nothing). Success -> `pdf`; every
candidate refused or none available -> `no-pdf` with a short reason in
`error`. A `pdf/<id>.pdf` already on disk is accepted without a download.
Contract: references/workflow.md.
"""
import concurrent.futures as cf
import contextlib
import http.client
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402
import access as A  # noqa: E402  (Europe PMC, publisher TDM APIs, institutional proxy)

MAILTO = "yushiranyushiran@gmail.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
BACKOFF = (2, 5)  # sleeps between the three attempts
NO_RETRY = {401, 402, 403, 404}
HOST_LIMITS = {"arxiv.org": 1}  # max concurrent requests per host
HOST_PAUSE = {"arxiv.org": 1.0}  # seconds to hold the slot after a request
# Publisher and proxy routes: one request at a time with a pause, as mining terms ask.
for _h in ("api.wiley.com", "api.elsevier.com", "www.ebi.ac.uk", "onlinelibrary.wiley.com",
           "ieeexplore.ieee.org", "www.sciencedirect.com", "link.springer.com"):
    HOST_LIMITS[_h], HOST_PAUSE[_h] = 1, 1.5
# EZproxy answers 200 with its login page when the session cookie is gone, so the
# only signal is the URL we ended up at.
PROXY_LOGIN_RE = re.compile(r"^https?://login\.[^/]*\.oclc\.org/|/login\?(?:qurl|url)=", re.I)
CREDENTIALED_VIA = ("ezproxy", "wiley-tdm", "elsevier-api")
PREPLACE_STATES = ("selected", "pdf", "no-pdf", "failed")
SAVE_EVERY = 5
CHUNK = 1 << 20
MAX_ERROR = 200
_UMASK = os.umask(0)  # read once in the main thread; mkstemp files start at 0600
os.umask(_UMASK)


class Refused(Exception):
    """Definitive failure: retrying this URL will not help."""


class Transient(Exception):
    """Failed after every retry; the server may answer later."""


class SessionExpired(Refused):
    """The institutional proxy bounced us to its login page: the cookie jar is stale."""


# ---------------------------------------------------------------- http

_host_sems, _host_lock = {}, threading.Lock()


def host_key(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return "arxiv.org" if host.endswith("arxiv.org") else host


def host_slot(key):
    limit = HOST_LIMITS.get(key)
    if limit is None:
        return contextlib.nullcontext()
    with _host_lock:
        return _host_sems.setdefault(key, threading.Semaphore(limit))


_opener_cache, _opener_lock = {}, threading.Lock()


def _opener():
    """Cookie-carrying opener when LITREV_COOKIES is set, else the default one."""
    with _opener_lock:
        if "o" not in _opener_cache:
            _opener_cache["o"] = A.cookie_opener()
        return _opener_cache["o"]


# def open_url(url, timeout):                                  # old: no per-candidate headers
def open_url(url, timeout, headers=None):
    h = {"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    opener = _opener()
    if opener is not None:
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def http_reason(code):
    if code in (402, 403):
        return f"{code} paywall"
    if code == 401:
        return "401 unauthorised"
    if code == 404:
        return "404 not found"
    return f"HTTP {code}"


def describe(e):
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        if isinstance(e.reason, str):
            return e.reason
        e = e.reason
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(e, socket.gaierror):
        return "dns failure"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    return f"{type(e).__name__}: {e}"[:80]


def with_retries(fn, url, timeout, what):
    """Run fn(url, timeout) up to three times. Refused passes through, transient errors back off."""
    key, last = host_key(url), None
    for attempt in range(len(BACKOFF) + 1):
        try:
            with host_slot(key):
                try:
                    return fn(url, timeout)
                finally:
                    if key in HOST_PAUSE:
                        time.sleep(HOST_PAUSE[key])
        except Refused:
            raise
        except urllib.error.HTTPError as e:
            if e.code in NO_RETRY or (400 <= e.code < 500 and e.code != 429):
                raise Refused(http_reason(e.code))
            last = http_reason(e.code)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            last = describe(e)
        if attempt < len(BACKOFF):
            M.log(f"fetch: {what}: {last}; retry in {BACKOFF[attempt]}s")
            time.sleep(BACKOFF[attempt])
    raise Transient(f"{last} after {len(BACKOFF) + 1} attempts")


# ---------------------------------------------------------------- pdf body

def body_kind(head):
    """'' when the bytes look like a PDF, else a short reason."""
    if not head:
        return "empty body"
    if b"%PDF" in head[:1024]:
        return ""
    probe = head[:512].lstrip().lower()
    if probe.startswith((b"<!doctype", b"<html")) or b"<html" in probe:
        return "html body"
    return "not a pdf"


def download(url, timeout, dest, headers=None):
    """Stream url into a temp file beside dest, check the magic, then os.replace. Returns bytes."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.stem}-", suffix=".part")
    done = False
    try:
        with os.fdopen(fd, "wb") as out, open_url(url, timeout, headers) as r:
            if PROXY_LOGIN_RE.search(r.geturl() or ""):
                raise SessionExpired("proxy session expired, re-export the cookie jar")
            head = r.read(CHUNK)
            kind = body_kind(head)
            if kind:
                raise Refused(kind)
            out.write(head)
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
            size = out.tell()
        os.chmod(tmp, 0o666 & ~_UMASK)
        os.replace(tmp, dest)
        done = True
        return size
    finally:
        if not done:
            with contextlib.suppress(OSError):
                os.remove(tmp)


def is_pdf_file(path):
    try:
        with open(path, "rb") as f:
            return body_kind(f.read(1024)) == ""
    except OSError:
        return False


# ---------------------------------------------------------------- candidates

def candidate_urls(paper):
    """Static candidates in order: pdf_url, arXiv. OpenAlex is looked up lazily.

    Each entry is (via, url, headers); headers is empty for these two.
    """
    out, seen = [], set()
    url = (paper.get("pdf_url") or "").strip()
    if url:
        # An arXiv abstract page is HTML; its /pdf/ twin is the file.
        url = re.sub(r"(arxiv\.org)/abs/", r"\1/pdf/", url, flags=re.IGNORECASE)
        out.append(("pdf_url", url, {}))
        seen.add(url)
    aid = M.norm_arxiv(paper.get("arxiv"))
    if aid:
        url = f"https://arxiv.org/pdf/{aid}"
        if url not in seen:
            out.append(("arxiv", url, {}))
    return out


def get_json(url, timeout, what):
    """Retrying GET returning parsed JSON. Raises Transient; Refused/ValueError -> {}."""
    def get(u, t):
        with open_url(u, t) as r:
            return json.loads(r.read())

    try:
        return with_retries(get, url, timeout, what)
    except (Refused, ValueError):
        return {}


# def openalex_pdf_url(doi, timeout, what):          # old: only the OA pdf link
def openalex_meta(doi, timeout, what):
    """(oa_pdf_url, landing_page_url, pmcid) for the DOI; empty strings when absent.

    The landing page and the PMC id are what the institutional and Europe PMC
    routes in access.py need, so they come from the same single lookup.
    """
    q = urllib.parse.quote(doi, safe="/:")
    url = (f"https://api.openalex.org/works/doi:{q}"
           f"?select=best_oa_location,primary_location,ids&mailto={MAILTO}")
    data = get_json(url, timeout, what)
    oa = (data.get("best_oa_location") or {}).get("pdf_url") or ""
    landing = (data.get("primary_location") or {}).get("landing_page_url") or ""
    pmcid = ""
    for key, val in (data.get("ids") or {}).items():
        if key == "pmcid" and val:
            pmcid = str(val).rstrip("/").split("/")[-1]
    return oa.strip(), landing.strip(), pmcid


def fetch_one(pid, paper, dest, timeout):
    """Worker: try every candidate for one paper. Returns a result dict, never raises."""
    reasons, n_refused, n_transient, tried, dead = [], 0, 0, set(), []

    def attempt(via, url, headers=None):
        nonlocal n_refused, n_transient
        if url in tried:
            return None
        tried.add(url)
        try:
            size = with_retries(lambda u, t: download(u, t, dest, headers), url, timeout, f"{pid} {via}")
            return {"id": pid, "status": "pdf", "via": via, "size": size, "error": ""}
        except SessionExpired as e:
            dead.append(via)
            reasons.append(f"{via}: {e}")
        except Refused as e:
            n_refused += 1
            reasons.append(f"{via}: {e}")
        except Transient as e:
            n_transient += 1
            reasons.append(f"{via}: {e}")
        return None

    for via, url, headers in candidate_urls(paper):
        r = attempt(via, url, headers)
        if r:
            return r
    doi = M.norm_doi(paper.get("doi"))
    landing, pmcid = "", ""
    if doi:
        try:
            url, landing, pmcid = openalex_meta(doi, timeout, f"{pid} openalex")
        except Transient as e:
            n_transient += 1
            reasons.append(f"openalex: {e}")
            url = ""
        else:
            if url:
                r = attempt("openalex", url)
                if r:
                    return r
    # Europe PMC, publisher mining APIs, institutional proxy: only reached when the
    # paper has no open-access copy. Each is a no-op unless its credential is set.
    try:
        extra = A.candidates(paper, landing, pmcid, lambda u: get_json(u, timeout, f"{pid} epmc"),
                             known_urls=(url, paper.get("pdf_url") or ""))
    except Exception as e:  # a broken route must never lose the paper
        extra, _ = [], reasons.append(f"access: {describe(e)}")
    for via, url, headers in extra:
        r = attempt(via, url, headers)
        if r:
            return r
    if not reasons:
        reasons.append("no OA link")
    error = "; ".join(reasons)
    if len(error) > MAX_ERROR:
        error = error[: MAX_ERROR - 3] + "..."
    return {
        "id": pid,
        "status": "no-pdf",
        "error": error,
        "transient": n_refused == 0 and n_transient > 0,
        "session_expired": bool(dead),
    }


# ---------------------------------------------------------------- main

def main():
    parser = M.base_parser("Download the open-access PDF of every selected paper.")
    parser.add_argument("--jobs", type=int, default=4, help="parallel downloads (default 4)")
    parser.add_argument("--timeout", type=float, default=60, help="per-request timeout in seconds (default 60)")
    parser.add_argument("--retry-no-pdf", action="store_true",
                        help="also retry papers already marked no-pdf (use after configuring "
                             "institutional access; see access.py)")
    args = parser.parse_args()
    if args.jobs < 1 or args.timeout <= 0:
        M.log("fetch: --jobs must be >= 1 and --timeout > 0")
        return M.EXIT_USAGE

    manifest = M.load(args)
    tdir = M.topic_dir(args)
    (tdir / "pdf").mkdir(exist_ok=True)

    def pdf_path(p):
        rel = p.get("pdf") or f"pdf/{p['id']}.pdf"
        p["pdf"] = rel
        return tdir / rel

    # PDFs the user dropped in by hand are accepted without a download.
    n_pre = 0
    for p in M.papers_in(manifest, *PREPLACE_STATES):
        path = pdf_path(p)
        if not path.is_file():
            continue
        if not is_pdf_file(path):
            M.log(f"fetch: {p['id']}: {path.name} exists but is not a PDF, ignored")
            continue
        if p["status"] != "pdf":
            p["status"], p["error"] = "pdf", ""
            n_pre += 1
            M.log(f"fetch: {p['id']}: pre-placed")

    if args.retry_no_pdf:
        for p in M.papers_in(manifest, "no-pdf"):
            p["status"], p["error"] = "selected", ""
        M.save(args, manifest)

    todo = M.papers_in(manifest, "selected")
    if not todo and not n_pre:
        M.log("fetch: nothing selected and no pre-placed pdf, nothing to do")
        print("pdf=0 no-pdf=0 pre-placed=0 remaining_selected=0")
        return M.EXIT_NOTHING
    if n_pre:
        M.save(args, manifest)
    M.log(f"fetch: {len(todo)} selected paper(s), {args.jobs} job(s)")
    M.log(f"fetch: extra routes for paywalled papers: {A.configured()}")

    papers = manifest["papers"]
    n_pdf, n_nopdf, transient, completed, session_dead = 0, 0, [], 0, False
    pool = cf.ThreadPoolExecutor(max_workers=args.jobs)
    try:
        futures = [pool.submit(fetch_one, p["id"], p, pdf_path(p), args.timeout) for p in todo]
        for fut in cf.as_completed(futures):
            r = fut.result()
            p = papers[r["id"]]
            if r.get("session_expired"):
                # Leave the paper `selected`: it is our session that failed, not the paper.
                session_dead = True
                M.log(f"fetch: {r['id']}: institutional proxy session expired")
                completed += 1
                continue
            if r["status"] == "pdf":
                p["status"], p["error"], p["pdf_via"] = "pdf", "", r["via"]
                n_pdf += 1
                M.log(f"fetch: {r['id']}: pdf via {r['via']} ({r['size'] / 1e6:.1f} MB)")
            elif r["transient"]:
                # Held back: decided once we know whether the network itself was down.
                transient.append(r)
                M.log(f"fetch: {r['id']}: unreachable ({r['error']})")
            else:
                p["status"], p["error"] = "no-pdf", r["error"]
                n_nopdf += 1
                M.log(f"fetch: {r['id']}: no-pdf ({r['error']})")
            completed += 1
            if completed % SAVE_EVERY == 0:
                M.save(args, manifest)
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        M.save(args, manifest)
        M.log("fetch: interrupted, progress saved")
        return M.EXIT_USAGE
    pool.shutdown(wait=True)

    network_down = bool(todo) and transient and len(transient) == len(todo)
    if not network_down:
        for r in transient:
            p = papers[r["id"]]
            p["status"], p["error"] = "no-pdf", r["error"]
            n_nopdf += 1
    M.save(args, manifest)

    remaining = len(M.papers_in(manifest, "selected"))
    print(f"pdf={n_pdf} no-pdf={n_nopdf} pre-placed={n_pre} remaining_selected={remaining}")
    if session_dead:
        M.log("fetch: the institutional proxy sent us to its login page. Re-export the cookie\n"
              "       jar from a freshly signed-in browser tab, then re-run with --retry-no-pdf.\n"
              f"       {remaining} paper(s) left selected; nothing was wrongly marked no-pdf.")
        return M.EXIT_NETWORK
    if network_down:
        M.log(f"fetch: every download failed on the wire; {remaining} paper(s) left selected for a re-run")
        return M.EXIT_NETWORK
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
