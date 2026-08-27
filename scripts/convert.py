# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Convert every `pdf` paper to Markdown with `mineru-open-api extract`.

One CLI subprocess per paper, N in parallel (the CLI's own --concurrency is
reserved and does nothing). Output lands in md/<id>/<id>.md with its images/
directory beside it. Success -> `md`; failure or timeout -> `failed` with the
last stderr line in `error`. The token gate runs before any paper is touched
and a rejected token stops the run with exit 3. Never falls back to
flash-extract. Contract: references/workflow.md.
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

def convert_one(pid: str, pdf_path: Path, out_dir: Path, args, abort: threading.Event) -> dict:
    res = {"id": pid, "status": "failed", "error": "", "stderr": ""}
    if abort.is_set():
        res["error"] = "not attempted: run aborted"
        res["status"] = "skipped"
        return res
    if not pdf_path.is_file():
        res["error"] = f"pdf not found on disk: {pdf_path.name}"
        return res
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        CLI, "extract", str(pdf_path.resolve()), "-o", str(out_dir.resolve()) + os.sep,
        "-f", "md", "--language", args.language, "--timeout", str(int(args.timeout)),
    ]
    if args.model != "auto":
        cmd += ["--model", args.model]
    M.log(f"convert: {pid}: start")
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

    # Already-converted papers are accepted without a run.
    n_skip, todo = 0, []
    for p in M.papers_in(manifest, *states):
        md = md_path(p)
        if md.is_file() and md.stat().st_size > MIN_MD_BYTES:
            p["status"], p["error"] = "md", ""
            n_skip += 1
            M.log(f"convert: {p['id']}: already converted")
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
                M.log(f"convert: {r['id']}: md")
            else:
                p["status"], p["error"] = "failed", r["error"]
                M.log(f"convert: {r['id']}: failed ({r['error']})")
            counts[r["status"]] += 1
            M.save(args, fresh)

    def work(p):
        r = convert_one(p["id"], pdf_path(p), tdir / "md" / p["id"], args, abort)
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
