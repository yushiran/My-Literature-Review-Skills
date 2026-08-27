# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Apply the scout's selected.json to the manifest.

Listed ids become `selected`; every other `found` paper that appears in
candidates.md becomes `rejected`. Papers already past selection are left alone.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

PAST_SELECTION = ("pdf", "no-pdf", "md", "failed")
CANDIDATE_LINE = re.compile(r"^## \d+\. (\S+)\s*$")


def load_selection(path: Path) -> list:
    """Return [(id, why)] from selected.json, raising ValueError on a bad shape."""
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError("selected.json must be a JSON array")
    out, seen = [], set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ValueError(f"entry {i} must be an object with a string 'id'")
        pid = entry["id"].strip()
        if pid in seen:
            M.log(f"select: duplicate id {pid} in selection, using the first")
            continue
        seen.add(pid)
        out.append((pid, str(entry.get("why") or "")))
    return out


def candidate_ids(path: Path) -> set:
    if not path.is_file():
        return set()
    ids = set()
    for line in path.read_text().splitlines():
        m = CANDIDATE_LINE.match(line)
        if m:
            ids.add(m.group(1))
    return ids


def main() -> int:
    parser = M.base_parser("Apply selected.json: mark selected, reject the other candidates.")
    parser.add_argument("--file", required=True, help="selected.json: array of {id, why}")
    parser.add_argument("--target", type=int, default=30, help="expected batch size, warn if exceeded (default 30)")
    args = parser.parse_args()

    sel_path = Path(args.file)
    if not sel_path.is_file():
        M.log(f"select: file not found: {sel_path}")
        return M.EXIT_USAGE
    try:
        selection = load_selection(sel_path)
    except (ValueError, json.JSONDecodeError) as e:
        M.log(f"select: cannot read {sel_path}: {e}")
        return M.EXIT_USAGE
    if not selection:
        M.log(f"select: {sel_path} lists no ids, nothing to do")
        return M.EXIT_NOTHING
    if len(selection) > args.target:
        M.log(f"select: {len(selection)} ids selected, target is {args.target}")

    manifest = M.load(args)
    papers = manifest["papers"]
    missing = [pid for pid, _ in selection if pid not in papers]
    if missing:
        M.log(f"select: {len(missing)} id(s) not in manifest: {', '.join(missing)}")
        return M.EXIT_USAGE

    selected_ids, n_selected = set(), 0
    for pid, why in selection:
        p = papers[pid]
        if p.get("status") in PAST_SELECTION:
            M.log(f"select: {pid} already {p['status']}, left alone")
            continue
        p["status"] = "selected"
        p["why"] = why
        selected_ids.add(pid)
        n_selected += 1

    cand_path = M.topic_dir(args) / "candidates.md"
    cands = candidate_ids(cand_path)
    if not cands:
        M.log(f"select: no candidates found in {cand_path}, rejecting nothing")
    n_rejected = 0
    for p in M.papers_in(manifest, "found"):
        if p["id"] in cands and p["id"] not in selected_ids:
            p["status"] = "rejected"
            n_rejected += 1

    M.save(args, manifest)
    n_untouched = len(papers) - n_selected - n_rejected
    M.log(f"select: selected {n_selected}, rejected {n_rejected}, untouched {n_untouched}")
    print(f"selected={n_selected} rejected={n_rejected} untouched={n_untouched}")
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
