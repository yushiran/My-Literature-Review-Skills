"""Shared helpers for the literature-review scripts.

One manifest.json per topic is the single source of truth; every script loads
it, changes only the papers in its input state, and saves atomically. A save
merges: it re-reads the file under a short lock and applies only this process's
own changes, so steps run in parallel do not overwrite each other. Schema and
state machine: references/workflow.md.
"""
import argparse
import contextlib
import copy
import datetime as _dt
import fcntl
import json
import os
import re
import stat
import sys
import tempfile
import time
import unicodedata
import weakref
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
ENV_FILE = "~/.config/litrev/access.env"
# The keys load_env will take from the credential file; not all of them are secrets.
# old: SECRETS = ("OPENALEX_API_KEY", "S2_API_KEY", "WILEY_TDM_TOKEN", "ELSEVIER_API_KEY",
# old:            "ELSEVIER_INSTTOKEN", "LITREV_EZPROXY_HOST", "LITREV_COOKIES", "SPRINGER_API_KEY")
SECRETS = ("OPENALEX_API_KEY", "S2_API_KEY", "MINERU_TOKEN", "WILEY_TDM_TOKEN", "ELSEVIER_API_KEY",
           "ELSEVIER_INSTTOKEN", "LITREV_EZPROXY_HOST", "LITREV_COOKIES", "LITREV_VERIFY_URL",
           "SPRINGER_API_KEY")


def load_env(path=None):
    """Populate os.environ from a KEY=value file, without overriding a real export.

    Refuses a group- or world-readable file: an API key is a credential.
    """
    path = path or os.environ.get("LITREV_ENV_FILE") or os.path.expanduser(ENV_FILE)
    if not os.path.isfile(path):
        return
    # old: if os.stat(path).st_mode & (stat.S_IRGRP | stat.S_IROTH):
    # old:     print(f"load_env: {path} is readable by others; chmod 600 it. Ignored.", flush=True)
    # old:     return
    try:
        # inside the try: the file can vanish between isfile() and stat()
        if os.stat(path).st_mode & (stat.S_IRGRP | stat.S_IROTH):
            log(f"load_env: {path} is readable by others; chmod 600 it. Ignored.")
            return
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                # old: key, val = key.strip().removeprefix("export "), val.strip().strip("'\"")
                key, val = key.strip().removeprefix("export "), val.strip()
                # a trailing # is a comment only when the value is unquoted; quoted stays verbatim
                val = val.strip("'\"") if val[:1] in ("'", '"') else re.sub(r"\s+#.*$", "", val)
                if key in SECRETS and val and not os.environ.get(key):
                    os.environ[key] = val
    except OSError:
        pass


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


LIST_KEYS = ("rounds", "seeds", "queries")   # append-only logs; every other top-level key is a scalar
SET_LIKE = ("seeds", "queries")              # ... and these two hold no duplicates, as search.py enforces too


class _Manifest(dict):
    """What load() hands out. A dict subclass only because a plain dict cannot be
    weak-referenced, and save() needs to find the snapshot for this exact object."""
    __slots__ = ("__weakref__",)


_SNAPSHOTS = {}   # id(manifest) -> (weakref to it, its path, its state as loaded)


def _remember(m: _Manifest, path: Path) -> _Manifest:
    for k, (ref, _, _) in list(_SNAPSHOTS.items()):
        if ref() is None:
            del _SNAPSHOTS[k]   # load() is the only thing that grows this, so pruning here bounds it
    _SNAPSHOTS[id(m)] = (weakref.ref(m), str(path), copy.deepcopy(dict(m)))
    return m


def load(args) -> dict:
    """The manifest, plus a private snapshot of it that save() diffs against.

    A malformed file exits EXIT_USAGE with one line rather than a traceback, the
    way rank.py and select.py already handle a malformed triage.json.
    """
    p = manifest_path(args)
    if p.exists():
        # old: return json.loads(p.read_text())
        try:
            state = json.loads(p.read_text())
            if not isinstance(state, dict) or not isinstance(state.get("papers"), dict):
                raise ValueError("no 'papers' object; is this a manifest?")
        except (OSError, ValueError) as e:   # JSONDecodeError is a ValueError: a truncated file lands here
            log(f"manifest: cannot read {p}: {e}")
            sys.exit(EXIT_USAGE)
    else:
        state = {
            "topic": args.topic,
            "created": _dt.date.today().isoformat(),
            "questions": [],
            "queries": [],
            "since": None,
            "papers": {},
        }
    return _remember(_Manifest(state), p)


_LOCK_WARNED = False


@contextlib.contextmanager
def _manifest_lock(path: Path):
    """Exclusive for the milliseconds of one merge-and-write, never for a whole run.

    Beside the manifest rather than in LITREV_LOCK_DIR: two users sharing one library
    root have different caches and would take two different locks. Opened read-only,
    because flock needs no write bit and the second user may not own the file.
    """
    global _LOCK_WARNED
    fd = None
    try:
        fd = os.open(str(path) + ".lock", os.O_RDONLY | os.O_CREAT, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as e:
        # Read-only or lockless filesystem. The merge still re-reads the file here, so
        # the window shrinks from the whole run to this one write instead of vanishing.
        if not _LOCK_WARNED:
            _LOCK_WARNED = True
            log(f"manifest: no lock on {path} ({e}); saves are merged but not serialised")
        if fd is not None:
            os.close(fd)
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)   # closing the descriptor releases the flock


def _merge_into(disk: dict, snap, cur: dict) -> dict:
    """Apply the changes cur made since snap onto disk, in place, and return disk.

    snap advances to exactly what this save writes, so the next save of the same
    manifest carries only what changed after it. Without that a long-running writer
    re-asserts its old values at every save and undoes whatever moved on meanwhile.
    """
    # No snapshot (a hand-built dict): treat every difference from disk as ours, which
    # keeps the other writer's papers and fields instead of dropping them.
    base = snap if snap is not None else disk
    on_disk = disk.setdefault("papers", {})
    was_papers = base.setdefault("papers", {}) if snap is not None else on_disk
    for pid, p in (cur.get("papers") or {}).items():
        was, now = was_papers.get(pid), on_disk.get(pid)
        if was is None or not isinstance(now, dict):
            on_disk[pid] = was_papers[pid] = copy.deepcopy(p)   # we added it, or disk holds no usable copy
            continue
        for k, v in p.items():
            if k not in was or was[k] != v:
                now[k] = was[k] = v   # only the fields we changed, so another writer's fields survive
        if snap is not None:
            for k in list(was):       # convert.py drops paper['conversion'] on a full re-conversion
                if k not in p:
                    now.pop(k, None)
                    was.pop(k, None)
    # A paper on disk that we never loaded is left alone; nothing in this codebase deletes one.
    for k in LIST_KEYS:
        mine = cur.get(k)
        if not isinstance(mine, list):
            continue
        have = disk.get(k)
        if not isinstance(have, list):
            have = disk[k] = []
        was = base.get(k)
        if not isinstance(was, list):
            was = base[k] = []        # with no snapshot base is disk, so this is `have` and stays one list
        for item in mine:
            if item in was:
                continue              # already written, by an earlier save of this same manifest
            if not (k in SET_LIKE and item in have):
                have.append(item)     # seeds and queries are sets: search.py never appends a duplicate either
            if was is not have:
                was.append(item)
    for k, v in cur.items():
        if k == "papers" or k in LIST_KEYS:
            continue
        if k not in base or base[k] != v:
            disk[k] = base[k] = v     # a scalar we only read stays as the other writer left it
    return disk


def save(args, manifest: dict) -> None:
    """Merge this process's own changes into the file as it stands now, atomically.

    A whole-file write loses everything another process did during our read-modify-write
    window, and those windows are minutes to hours: a fetch run, a snowball traversal.
    So we take a short exclusive lock, re-read, apply only the diff against what load()
    handed us, write, and release.

    The limit, stated rather than hidden: if two processes change the SAME field of the
    SAME paper, the later save wins. A real transaction is the only way round that, and
    the blast radius here is one field instead of another process's entire run.
    """
    p = manifest_path(args)
    entry = _SNAPSHOTS.get(id(manifest))
    # Identity, not just id(): a dead manifest's address can be reused by another object.
    snap = entry[2] if entry and entry[0]() is manifest and entry[1] == str(p) else None
    with _manifest_lock(p):
        disk = None
        if p.exists():
            try:
                disk = json.loads(p.read_text())
            except (OSError, ValueError) as e:
                log(f"manifest: {p} is unreadable, replacing it with this run's copy ({e})")
        # A diff carries only changes, so an absent file has to be written whole.
        out = _merge_into(disk, snap, manifest) if isinstance(disk, dict) else manifest
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".manifest-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(out, f, indent=1, ensure_ascii=False)
        os.chmod(tmp, 0o666 & ~UMASK)  # mkstemp files start at 0600
        os.replace(tmp, p)
    if out is manifest and snap is not None:
        snap.clear()                   # all of it is on disk now, so a later save carries only what follows
        snap.update(copy.deepcopy(dict(manifest)))


@contextlib.contextmanager
def host_gate(host: str, min_interval: float = 0.0):
    """One request at a time per host across every process on this machine, at least
    min_interval seconds apart. arXiv asks for 3 s; three parallel searches produced
    nothing but 429s without this."""
    lock_dir = os.environ.get("LITREV_LOCK_DIR") or os.path.expanduser("~/.cache/litrev/locks")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, host.replace("/", "_") + ".lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lock.seek(0)
            last = float(lock.read().strip() or 0)
            wait = last + min_interval - time.time()
            if wait > 0:
                time.sleep(wait)
            yield
        finally:
            lock.seek(0)
            lock.truncate()
            lock.write(str(time.time()))
            lock.flush()
            fcntl.flock(lock, fcntl.LOCK_UN)


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


def as_list(v) -> list:
    """A hand-edited list field, for iterating. A bare string is the one likely
    malformation, and iterating it yields one item per character."""
    return v if isinstance(v, list) else ([v] if isinstance(v, str) and v else [])


def today() -> str:
    return _dt.date.today().isoformat()


def fuzzy_title(title: str) -> str:
    """norm_title after fusing hyphenated words: 'multi-modal' == 'multimodal'."""
    return norm_title((title or "").replace("-", ""))


def title_tokens(title: str) -> set:
    return {t for t in ascii_slug(title).split() if t not in STOPWORDS and len(t) > 1}


def new_paper(pid: str, **fields) -> dict:
    p = {
        "id": pid, "title": "", "authors": [], "year": None, "venue": "",
        "venue_tier": 3, "doi": "", "arxiv": "", "openalex": "", "s2": "",
        "citations": 0, "abstract": "", "pdf_url": "", "sources": [],
        "relevance": None,  # OpenAlex relevance_score, or null for S2/arXiv-only hits
        "score": 0.0, "status": "found", "why": "",
        "pdf": f"pdf/{pid}.pdf", "md": f"md/{pid}/{pid}.md", "error": "",
        "found_via": [], "found_date": today(), "snowball_hits": 0,
    }
    p.update({k: v for k, v in fields.items() if v is not None})
    return p


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
