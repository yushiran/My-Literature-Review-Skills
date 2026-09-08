# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Convert every `pdf` paper to Markdown with `mineru-open-api extract`.

One CLI subprocess per paper, N in parallel (the CLI's own --concurrency is
reserved and does nothing). A paper with an arXiv id or an open-access
`pdf_url` is handed to the CLI as a URL, so the MinerU server fetches it
itself and nothing is uploaded from this machine (uploads to the MinerU OSS
bucket time out from many HPC / campus networks). The local pdf/<id>.pdf is
uploaded only when no URL exists or the URL attempt failed; `--upload` forces
the local file. Output lands in md/<id>/<id>.md with its images/ directory
beside it. Success -> `md`; failure or timeout -> `failed` with the last
stderr line in `error`. The token gate runs before any paper is touched and a
rejected token stops the run with exit 3. Never falls back to flash-extract.
Contract: references/workflow.md.
"""
import concurrent.futures as cf
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

CLI = "mineru-open-api"
TOKEN_MSG = (
    "MinerU token missing. Create one at https://mineru.net/apiManage/token "
    "then run: mineru-open-api auth   (or export MINERU_TOKEN=...)"
)
NPM_MSG = "mineru-open-api not found: npm install -g mineru-open-api"
AUTH_TIMEOUT = 20
MIN_MD_BYTES = 200
MAX_ERROR = 300
TOKEN_REJECTED = re.compile(r"\b40[13]\b|token|unauthori[sz]ed", re.I)
# ![alt](path) and <img src="path">
MD_LINK = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)(\))|(<img\b[^>]*?\bsrc=[\"'])([^\"']+)([\"'])")


class TokenRejected(Exception):
    """The API refused the token mid-run; nothing else will succeed."""


# ---------------------------------------------------------------- token gate

def token_gate() -> str:
    """Return '' when a token is available, else the message to print before exit 3."""
    if os.environ.get("MINERU_TOKEN"):
        return ""
    try:
        r = subprocess.run(
            [CLI, "auth", "--show"], capture_output=True, text=True,
            timeout=AUTH_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return NPM_MSG
    except subprocess.TimeoutExpired:
        M.log(f"convert: '{CLI} auth --show' did not answer within {AUTH_TIMEOUT}s")
        return TOKEN_MSG
    out = (r.stdout or "") + (r.stderr or "")
    if "Token source" not in out or "not configured" in out or "No token" in out:
        return TOKEN_MSG
    return ""


# ---------------------------------------------------------------- output layout

def last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:MAX_ERROR] if lines else ""


def hoist(src: Path, dst: Path) -> None:
    """Move everything in src up into dst, merging directories, then drop src."""
    for item in list(src.iterdir()):
        target = dst / item.name
        if item.is_dir() and target.is_dir():
            hoist(item, target)
        elif target.exists():
            if item.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            os.replace(item, target)
        else:
            os.replace(item, target)
    try:
        src.rmdir()
    except OSError:
        pass


def fix_links(md_path: Path) -> int:
    """Repoint image links that broke when the md was hoisted; return the count."""
    base = md_path.parent
    text = md_path.read_text(errors="replace")
    changed = 0

    def repl(m):
        nonlocal changed
        if m.group(2) is not None:
            pre, link, post = m.group(1), m.group(2), m.group(3)
        else:
            pre, link, post = m.group(4), m.group(5), m.group(6)
        if link.startswith(("http://", "https://", "data:")) or (base / link).exists():
            return m.group(0)
        parts = Path(link).parts
        for i in range(1, len(parts)):
            cand = Path(*parts[i:])
            if (base / cand).exists():
                changed += 1
                return pre + cand.as_posix() + post
        return m.group(0)

    new = MD_LINK.sub(repl, text)
    if changed:
        md_path.write_text(new)
    return changed


def finalize(out_dir: Path, pid: str):
    """Put the CLI's output at md/<id>/<id>.md with assets beside it; return the md or None."""
    target = out_dir / f"{pid}.md"
    mds = [p for p in out_dir.rglob("*.md") if p.is_file()]
    if not mds:
        return None
    best = max(mds, key=lambda p: p.stat().st_size)
    src_dir = best.parent
    if best.resolve() != target.resolve():
        os.replace(best, target)
    if src_dir.resolve() != out_dir.resolve():
        hoist(src_dir, out_dir)
    # An images/ dir left one level down (md at top, assets nested) comes up too.
    if not (out_dir / "images").exists():
        nested = [d for d in out_dir.rglob("images") if d.is_dir()]
        if nested:
            os.replace(nested[0], out_dir / "images")
            parent = nested[0].parent
            while parent.resolve() != out_dir.resolve() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    n = fix_links(target)
    if n:
        M.log(f"convert: {pid}: rewrote {n} image link(s) after hoisting")
    return target


# ---------------------------------------------------------------- one paper

CREDENTIALED_VIA = ("ezproxy", "wiley-tdm", "elsevier-api")

# Last-resort local extraction. On networks where the upload to MinerU's bucket
# does not complete, a paper we hold on disk would otherwise end `failed` with no
# full text at all, which is worse than text without figures. Opt-in, and every
# entry it writes says what it is so it is never mistaken for a MinerU conversion.
LOCAL_TEXT_PROGRAM = """
import sys, pymupdf
src, dst = sys.argv[1], sys.argv[2]
doc = pymupdf.open(src)
head = ['<!-- Local text extraction (pymupdf), not a MinerU conversion.',
        '     Every word of the text is here; figures, images, table structure',
        '     and formula markup are not. -->', '']
body = [page.get_text('text') for page in doc]
if sum(len(b.strip()) for b in body) < 500:
    sys.exit('no extractable text layer (scanned pdf?)')
open(dst, 'w').write('\\n'.join(head + body))
print(f'{len(doc)} pages')
"""


def local_text_md(pdf: Path, md: Path, timeout: float) -> str:
    """Write a text-only md from a local pdf. Returns '' on success, else the reason."""
    md.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["uv", "run", "--with", "pymupdf", "python", "-", str(pdf), str(md)]
    try:
        r = subprocess.run(cmd, input=LOCAL_TEXT_PROGRAM, capture_output=True,
                           text=True, timeout=timeout)
    except FileNotFoundError:
        return "local text: uv not on PATH"
    except subprocess.TimeoutExpired:
        return "local text: timed out"
    if r.returncode != 0:
        return "local text: " + (last_line(r.stderr) or f"exit {r.returncode}")
    if not md.is_file() or md.stat().st_size < MIN_MD_BYTES:
        return "local text: produced nothing usable"
    return ""


def sources_of(p: dict, pdf_path: Path, upload_only: bool) -> list:
    """Ordered (mode, source) attempts: arXiv URL, then pdf_url, then the local file.

    A PDF that only came down through the institutional proxy or a mining API must
    be uploaded. MinerU fetches a URL from its own servers, which hold none of our
    credentials, so handing it the publisher link would convert a login or paywall
    page into plausible-looking markdown and file it as the paper.
    """
    if p.get("pdf_via") in CREDENTIALED_VIA:
        upload_only = True
    out = []
    if not upload_only:
        if p.get("arxiv"):
            out.append(("url", f"https://arxiv.org/pdf/{p['arxiv']}"))
        url = p.get("pdf_url") or ""
        if url.startswith(("http://", "https://")):
            out.append(("url", url))
    if pdf_path.is_file():
        out.append(("upload", str(pdf_path.resolve())))
    return out


def convert_one(pid: str, mode: str, source: str, out_dir: Path, args, abort: threading.Event) -> dict:
    """One CLI run; `source` is a URL the MinerU server fetches or a local pdf path it uploads."""
    res = {"id": pid, "status": "failed", "error": "", "stderr": ""}
    if abort.is_set():
        res["error"] = "not attempted: run aborted"
        res["status"] = "skipped"
        return res
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        CLI, "extract", source, "-o", str(out_dir.resolve()) + os.sep,
        "-f", "md", "--language", args.language, "--timeout", str(int(args.timeout)),
    ]
    if args.model != "auto":
        cmd += ["--model", args.model]
    M.log(f"convert: {pid}: start ({mode}: {source if mode == 'url' else Path(source).name})")
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=args.timeout, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        res["error"] = NPM_MSG
        return res
    except subprocess.TimeoutExpired as e:
        err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        res["error"] = f"timeout after {int(args.timeout)}s"
        res["stderr"] = err
        return res
    res["stderr"] = r.stderr or ""
    err = last_line(r.stderr) or last_line(r.stdout) or f"exit code {r.returncode}"
    if r.returncode != 0 and TOKEN_REJECTED.search(r.stderr or ""):
        res["status"] = "token"
        res["error"] = err
        return res
    md = finalize(out_dir, pid)
    if md is not None and md.stat().st_size > MIN_MD_BYTES:
        res["status"], res["error"] = "md", ""
        return res
    if r.returncode != 0:
        res["error"] = err
    elif md is None:
        res["error"] = "no .md produced in output dir"
    else:
        res["error"] = f"md too small ({md.stat().st_size} bytes)"
    return res


# ---------------------------------------------------------------- main

def main() -> int:
    parser = M.base_parser("Convert `pdf` papers to Markdown with mineru-open-api extract.")
    parser.add_argument("--jobs", type=int, default=4, help="parallel conversions (default 4)")
    parser.add_argument("--model", choices=("vlm", "pipeline", "auto"), default="auto",
                        help="MinerU model; auto = let the CLI decide (default auto)")
    parser.add_argument("--language", default="en", help="document language (default en)")
    parser.add_argument("--timeout", type=float, default=900, help="per-file timeout in seconds (default 900)")
    parser.add_argument("--retry-failed", action="store_true", help="also re-run papers in state failed")
    parser.add_argument("--local-text-fallback", action="store_true",
                        help="when MinerU cannot fetch or accept a paper, extract its text locally "
                             "with pymupdf; keeps every word, loses figures and tables, and the entry "
                             "is marked conversion=local-text")
    parser.add_argument("--upload", action="store_true",
                        help="always upload the local pdf; skip the arXiv / pdf_url URL attempts")
    args = parser.parse_args()
    if args.jobs < 1:
        M.log("convert: --jobs must be >= 1")
        return M.EXIT_USAGE

    msg = token_gate()
    if msg:
        M.log(msg)
        return M.EXIT_TOKEN

    manifest = M.load(args)
    tdir = M.topic_dir(args)
    (tdir / "md").mkdir(exist_ok=True)
    states = ("pdf", "failed") if args.retry_failed else ("pdf",)

    def md_path(p):
        p["md"] = f"md/{p['id']}/{p['id']}.md"
        return tdir / p["md"]

    def pdf_path(p):
        rel = p.get("pdf") or f"pdf/{p['id']}.pdf"
        p["pdf"] = rel
        return tdir / rel

    # Already-converted papers are accepted without a run. A `no-pdf` paper is
    # attempted too when it has a URL the MinerU server can fetch itself.
    n_skip, todo = 0, []
    for p in M.papers_in(manifest, *states, "no-pdf"):
        md = md_path(p)
        if md.is_file() and md.stat().st_size > MIN_MD_BYTES:
            p["status"], p["error"] = "md", ""
            n_skip += 1
            M.log(f"convert: {p['id']}: already converted")
        elif p["status"] == "no-pdf" and (args.upload or not sources_of(p, pdf_path(p), False)):
            continue  # nothing the server could fetch; stays no-pdf with its abstract
        else:
            todo.append(p)
    if n_skip:
        M.save(args, manifest)
    if not todo:
        remaining = len(M.papers_in(manifest, "pdf"))
        M.log("convert: nothing to convert")
        print(f"md=0 failed=0 skipped={n_skip} remaining_pdf={remaining}")
        return M.EXIT_OK if n_skip else M.EXIT_NOTHING
    M.log(f"convert: {len(todo)} paper(s), {args.jobs} job(s), model={args.model}")

    lock = threading.Lock()
    abort = threading.Event()
    counts = {"md": 0, "failed": 0, "skipped": n_skip}
    token_err = []

    def record(r: dict) -> None:
        # Reload, change one paper, save: parallel completions never clobber each other.
        with lock:
            if r["status"] == "skipped":
                return
            if r["status"] == "token":
                abort.set()
                token_err.append(r["error"])
                M.log(f"convert: {r['id']}: token rejected ({r['error']})")
                return
            fresh = M.load(args)
            p = fresh["papers"].get(r["id"])
            if p is None:
                return
            p["md"] = f"md/{r['id']}/{r['id']}.md"
            if r["status"] == "md":
                p["status"], p["error"] = "md", ""
                if r.get("local_text"):
                    p["conversion"] = "local-text"   # no figures/tables; see local_text_md
                    M.log(f"convert: {r['id']}: md (local text only)")
                else:
                    p.pop("conversion", None)
                    M.log(f"convert: {r['id']}: md")
            elif p["status"] == "no-pdf":
                p["error"] = r["error"]  # URL attempt failed; keep no-pdf, not failed
                M.log(f"convert: {r['id']}: still no-pdf ({r['error']})")
                M.save(args, fresh)
                return  # not counted as failed
            else:
                p["status"], p["error"] = "failed", r["error"]
                M.log(f"convert: {r['id']}: failed ({r['error']})")
            counts[r["status"]] += 1
            M.save(args, fresh)

    def work(p):
        # Try each source in order; stop at the first md (or a token rejection / abort).
        attempts = sources_of(p, pdf_path(p), args.upload)
        if not attempts:
            r = {"id": p["id"], "status": "failed", "error": f"pdf not found on disk: {p['id']}.pdf", "stderr": ""}
            record(r)
            return r
        errors = []
        for mode, source in attempts:
            r = convert_one(p["id"], mode, source, tdir / "md" / p["id"], args, abort)
            if r["status"] in ("md", "token", "skipped"):
                break
            errors.append(f"{mode}: {r['error']}")
        if r["status"] == "failed":
            r["error"] = " | ".join(errors)[:MAX_ERROR]
            # MinerU could neither fetch it nor accept the upload; read it locally.
            pdf = pdf_path(p)
            if args.local_text_fallback and pdf.is_file():
                why = local_text_md(pdf, md_path(p), args.timeout)
                if why:
                    r["error"] = (r["error"] + " | " + why)[:MAX_ERROR]
                else:
                    r["status"], r["local_text"] = "md", True
                    r["error"] = ""
        record(r)
        return r

    pool = cf.ThreadPoolExecutor(max_workers=args.jobs)
    try:
        futures = [pool.submit(work, p) for p in todo]
        for fut in cf.as_completed(futures):
            fut.result()
    except KeyboardInterrupt:
        pool.shutdown(wait=False, cancel_futures=True)
        M.log("convert: interrupted, progress saved")
        return M.EXIT_USAGE
    pool.shutdown(wait=True)

    remaining = len(M.papers_in(M.load(args), "pdf"))
    print(f"md={counts['md']} failed={counts['failed']} skipped={counts['skipped']} remaining_pdf={remaining}")
    if token_err:
        M.log(TOKEN_MSG)
        M.log(f"convert: CLI said: {token_err[0]}")
        return M.EXIT_TOKEN
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
