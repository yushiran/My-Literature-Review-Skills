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

MAILTO = "yushiranyushiran@gmail.com"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
BACKOFF = (2, 5)  # sleeps between the three attempts
NO_RETRY = {401, 402, 403, 404}
HOST_LIMITS = {"arxiv.org": 1}  # max concurrent requests per host
HOST_PAUSE = {"arxiv.org": 1.0}  # seconds to hold the slot after a request
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


def open_url(url, timeout):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.8"}
    )
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


def download(url, timeout, dest):
    """Stream url into a temp file beside dest, check the magic, then os.replace. Returns bytes."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.stem}-", suffix=".part")
    done = False
    try:
        with os.fdopen(fd, "wb") as out, open_url(url, timeout) as r:
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
    """Static candidates in order: pdf_url, arXiv. OpenAlex is looked up lazily."""
    out, seen = [], set()
    url = (paper.get("pdf_url") or "").strip()
    if url:
        # An arXiv abstract page is HTML; its /pdf/ twin is the file.
        url = re.sub(r"(arxiv\.org)/abs/", r"\1/pdf/", url, flags=re.IGNORECASE)
        out.append(("pdf_url", url))
        seen.add(url)
    aid = M.norm_arxiv(paper.get("arxiv"))
    if aid:
        url = f"https://arxiv.org/pdf/{aid}"
        if url not in seen:
            out.append(("arxiv", url))
    return out


def openalex_pdf_url(doi, timeout, what):
    """best_oa_location.pdf_url for the DOI, '' when OpenAlex has none. Raises Transient."""
    q = urllib.parse.quote(doi, safe="/:")
    url = f"https://api.openalex.org/works/doi:{q}?select=best_oa_location&mailto={MAILTO}"

    def get(u, t):
        with open_url(u, t) as r:
            return json.loads(r.read())

    try:
        data = with_retries(get, url, timeout, what)
    except Refused:
        return ""  # unknown DOI
    except ValueError:
        return ""
    loc = data.get("best_oa_location") or {}
    return (loc.get("pdf_url") or "").strip()


def fetch_one(pid, paper, dest, timeout):
    """Worker: try every candidate for one paper. Returns a result dict, never raises."""
    reasons, n_refused, n_transient, tried = [], 0, 0, set()

    def attempt(via, url):
        nonlocal n_refused, n_transient
        if url in tried:
            return None
        tried.add(url)
        try:
            size = with_retries(lambda u, t: download(u, t, dest), url, timeout, f"{pid} {via}")
            return {"id": pid, "status": "pdf", "via": via, "size": size, "error": ""}
        except Refused as e:
            n_refused += 1
            reasons.append(f"{via}: {e}")
        except Transient as e:
            n_transient += 1
            reasons.append(f"{via}: {e}")
        return None

    for via, url in candidate_urls(paper):
        r = attempt(via, url)
        if r:
            return r
    doi = M.norm_doi(paper.get("doi"))
    if doi:
        try:
            url = openalex_pdf_url(doi, timeout, f"{pid} openalex")
        except Transient as e:
            n_transient += 1
            reasons.append(f"openalex: {e}")
            url = ""
        else:
            if url:
                r = attempt("openalex", url)
                if r:
                    return r
            else:
                n_refused += 1
                reasons.append("no OA link")
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
    }


# ---------------------------------------------------------------- main

def main():
    parser = M.base_parser("Download the open-access PDF of every selected paper.")
    parser.add_argument("--jobs", type=int, default=4, help="parallel downloads (default 4)")
    parser.add_argument("--timeout", type=float, default=60, help="per-request timeout in seconds (default 60)")
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

    todo = M.papers_in(manifest, "selected")
    if not todo and not n_pre:
        M.log("fetch: nothing selected and no pre-placed pdf, nothing to do")
        print("pdf=0 no-pdf=0 pre-placed=0 remaining_selected=0")
        return M.EXIT_NOTHING
    if n_pre:
        M.save(args, manifest)
    M.log(f"fetch: {len(todo)} selected paper(s), {args.jobs} job(s)")

    papers = manifest["papers"]
    n_pdf, n_nopdf, transient, completed = 0, 0, [], 0
    pool = cf.ThreadPoolExecutor(max_workers=args.jobs)
    try:
        futures = [pool.submit(fetch_one, p["id"], p, pdf_path(p), args.timeout) for p in todo]
        for fut in cf.as_completed(futures):
            r = fut.result()
            p = papers[r["id"]]
            if r["status"] == "pdf":
                p["status"], p["error"] = "pdf", ""
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
    if network_down:
        M.log(f"fetch: every download failed on the wire; {remaining} paper(s) left selected for a re-run")
        return M.EXIT_NETWORK
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
