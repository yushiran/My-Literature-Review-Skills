# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Extra download routes for papers with no open-access copy.

fetch.py tries pdf_url, arXiv and the OpenAlex OA location; anything behind a
paywall becomes `no-pdf`. This module adds three further routes, each opt-in and
each returning candidates in the same `(via, url, headers)` shape:

1. Europe PMC / PubMed Central. Free, no credentials. A large share of the
   MR-physics literature (MRM, NeuroImage, TMI) is deposited there after an
   embargo. Always tried.
2. Publisher text-and-data-mining APIs. Wiley (Magnetic Resonance in Medicine,
   NMR in Biomedicine, JMRI) and Elsevier (NeuroImage, Medical Image Analysis,
   Magnetic Resonance Imaging) both issue a token to subscribing institutions
   precisely so that their content can be fetched programmatically.
3. An institutional EZproxy. Works for anything the subscription covers,
   including IEEE (TMI, ISBI, TBME), which has no mining API. Needs a browser
   cookie jar exported after logging in once.

Configuration is by environment variable; nothing here is written to disk and no
token or cookie is ever logged.

    LITREV_EZPROXY_HOST   e.g. bris.idm.oclc.org
    LITREV_COOKIES        path to a Netscape cookies.txt exported from the
                          browser after signing in through the proxy
    WILEY_TDM_TOKEN       Wiley text-and-data-mining client token
    ELSEVIER_API_KEY      key from dev.elsevier.com
    ELSEVIER_INSTTOKEN    institutional token, needed when the request does not
                          come from a subscribing IP range

Rather than exporting them, put those lines in ~/.config/litrev/access.env
(mode 0600, `KEY=value` per line, `#` comments allowed) and this module reads it
on import. That way the secret never has to be typed into a shell that logs its
history, and it survives across separate script invocations.

Use only your own entitlement, and keep the concurrency low: fetch.py caps
these hosts at one request at a time and pauses between them, which is what
publisher mining terms ask for.
"""
import http.cookiejar
import json
import os
import re
import stat
import urllib.parse
import urllib.request

EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
ENV_FILE = os.environ.get("LITREV_ENV_FILE") or os.path.expanduser("~/.config/litrev/access.env")
_SECRETS = ("WILEY_TDM_TOKEN", "ELSEVIER_API_KEY", "ELSEVIER_INSTTOKEN",
            "LITREV_EZPROXY_HOST", "LITREV_COOKIES", "SPRINGER_API_KEY")


def _load_env_file(path=ENV_FILE):
    """Populate os.environ from a KEY=value file, without overriding a real export.

    Refuses a world- or group-readable file: a publisher token is a credential.
    """
    if not os.path.isfile(path):
        return
    mode = os.stat(path).st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(f"access: {path} is readable by others; chmod 600 it. Ignored.", flush=True)
        return
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip().removeprefix("export "), val.strip().strip("'\"")
                if key in _SECRETS and val and not os.environ.get(key):
                    os.environ[key] = val
    except OSError:
        pass


_load_env_file()

# Landing page -> direct PDF, for the publishers whose PDF path is derivable.
# Each entry: (host fragment, builder(landing_url, doi) -> url or "")
_PDF_RULES = []


def _rule(fragment):
    def deco(fn):
        _PDF_RULES.append((fragment, fn))
        return fn
    return deco


@_rule("onlinelibrary.wiley.com")
def _wiley(landing, doi):
    return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}" if doi else ""


@_rule("link.springer.com")
def _springer(landing, doi):
    return f"https://link.springer.com/content/pdf/{doi}.pdf" if doi else ""


@_rule("nature.com")
def _nature(landing, doi):
    m = re.search(r"/articles/([^/?#]+)", landing)
    return f"https://www.nature.com/articles/{m.group(1)}.pdf" if m else ""


@_rule("ieeexplore.ieee.org")
def _ieee(landing, doi):
    m = re.search(r"/(?:document|abstract/document)/(\d+)", landing)
    if not m:
        return ""
    return f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={m.group(1)}"


@_rule("sciencedirect.com")
def _elsevier(landing, doi):
    m = re.search(r"/(?:pii|article/pii)/([A-Z0-9]+)", landing)
    return f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}/pdfft?isDTMRedir=true" if m else ""


@_rule("journals.sagepub.com")
def _sage(landing, doi):
    return f"https://journals.sagepub.com/doi/pdf/{doi}" if doi else ""


@_rule("tandfonline.com")
def _tandf(landing, doi):
    return f"https://www.tandfonline.com/doi/pdf/{doi}" if doi else ""


@_rule("iopscience.iop.org")
def _iop(landing, doi):
    return f"https://iopscience.iop.org/article/{doi}/pdf" if doi else ""


def pdf_from_landing(landing, doi):
    """Publisher PDF url derived from a landing page, or '' when the pattern is unknown."""
    host = (urllib.parse.urlparse(landing).hostname or "").lower()
    for fragment, build in _PDF_RULES:
        if fragment in host:
            return build(landing, doi)
    return ""


# ---------------------------------------------------------------- ezproxy

def ezproxy_host_rewrite(url, proxy_host):
    """https://onlinelibrary.wiley.com/x -> https://onlinelibrary-wiley-com.<proxy>/x

    OCLC EZproxy in host-rewrite mode maps every dot of the origin host to a dash
    and appends its own domain. This keeps the path, so a direct PDF link stays a
    direct PDF link, which the login?url= form does not.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.hostname or proxy_host in parts.hostname:
        return url
    return urllib.parse.urlunsplit((
        "https", f"{parts.hostname.replace('.', '-')}.{proxy_host}",
        parts.path, parts.query, parts.fragment,
    ))


def cookie_opener():
    """An opener carrying the exported browser cookies, or None when unconfigured."""
    path = (os.environ.get("LITREV_COOKIES") or "").strip()
    if not path or not os.path.isfile(path):
        return None
    jar = http.cookiejar.MozillaCookieJar()
    try:
        jar.load(path, ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        return None
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


# ---------------------------------------------------------------- europe pmc

def europepmc_pdf(doi, pmcid, fetch_json):
    """Open-access PDF url from Europe PMC, or ''. fetch_json(url) -> dict, may raise."""
    if not pmcid:
        if not doi:
            return ""
        q = urllib.parse.urlencode({"query": f'DOI:"{doi}"', "format": "json",
                                    "resultType": "core", "pageSize": 1})
        try:
            data = fetch_json(f"{EPMC_SEARCH}?{q}")
        except Exception:
            return ""
        hits = ((data.get("resultList") or {}).get("result") or [])
        if not hits:
            return ""
        rec = hits[0]
        if str(rec.get("isOpenAccess", "")).upper() != "Y":
            return ""
        pmcid = rec.get("pmcid") or ""
        for link in ((rec.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
            if link.get("documentStyle") == "pdf" and link.get("url"):
                return link["url"]
    if pmcid:
        return f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextPDF"
    return ""


# ---------------------------------------------------------------- candidates

def candidates(paper, landing_url="", pmcid="", fetch_json=None):
    """Extra (via, url, headers) candidates for one paper, most-likely first.

    landing_url is the publisher page (OpenAlex primary_location.landing_page_url);
    fetch_json is fetch.py's retrying JSON getter, used only for Europe PMC.
    """
    doi = (paper.get("doi") or "").strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    out = []

    if fetch_json is not None and (doi or pmcid):
        url = europepmc_pdf(doi, pmcid, fetch_json)
        if url:
            out.append(("europepmc", url, {}))

    if doi:
        token = (os.environ.get("WILEY_TDM_TOKEN") or "").strip()
        if token and doi.startswith("10.1002"):
            out.append((
                "wiley-tdm",
                f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{urllib.parse.quote(doi, safe='')}",
                {"Wiley-TDM-Client-Token": token},
            ))
        key = (os.environ.get("ELSEVIER_API_KEY") or "").strip()
        if key and doi.startswith("10.1016"):
            headers = {"X-ELS-APIKey": key, "Accept": "application/pdf"}
            inst = (os.environ.get("ELSEVIER_INSTTOKEN") or "").strip()
            if inst:
                headers["X-ELS-Insttoken"] = inst
            out.append((
                "elsevier-api",
                f"https://api.elsevier.com/content/article/doi/{doi}?view=FULL",
                headers,
            ))

    proxy = (os.environ.get("LITREV_EZPROXY_HOST") or "").strip()
    if proxy:
        direct = pdf_from_landing(landing_url, doi) if landing_url else ""
        if not direct and doi:
            # No landing page: let the proxy resolve the DOI and hope for a PDF redirect.
            direct = f"https://doi.org/{doi}"
        if direct:
            out.append(("ezproxy", ezproxy_host_rewrite(direct, proxy), {}))
    return out


def configured():
    """Short description of the routes that are switched on, for the fetch log."""
    on = ["europepmc"]
    if os.environ.get("WILEY_TDM_TOKEN"):
        on.append("wiley-tdm")
    if os.environ.get("ELSEVIER_API_KEY"):
        on.append("elsevier-api")
    if os.environ.get("LITREV_EZPROXY_HOST"):
        on.append("ezproxy" + ("+cookies" if os.environ.get("LITREV_COOKIES") else " (no cookie jar)"))
    return ", ".join(on)
