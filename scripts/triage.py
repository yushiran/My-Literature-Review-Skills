# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Merge the triage.<k>.json files that K scouts wrote over the parts of candidates_titles.md.

    uv run scripts/triage.py --topic <slug> --merge references/<slug>/triage.1.json references/<slug>/triage.2.json

Writes references/<slug>/triage.json (or --out) and checks the contract its two readers rely on
(rank.py --only keeps `keep` and `undecided`, select.py --triage rejects `drop`): every part is an
object with those keys and string ids, every id sits in exactly one list across all the parts, and,
when candidates_titles.md exists, every id it lists was triaged by some part. A violation is a usage
error naming the ids, so a scout that skipped a part of its file is caught here and not at select.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

TRIAGE_KEYS = ("keep", "drop", "undecided")
CANDIDATE_LINE = re.compile(r"^## \d+\. (\S+)\s*$")


def load_part(path: Path) -> dict:
    """One scout's triage file as {key: [ids]}, raising ValueError on a bad shape."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not set(data) & set(TRIAGE_KEYS):
        raise ValueError(f"no {' / '.join(TRIAGE_KEYS)} key")
    out = {}
    for key in TRIAGE_KEYS:
        ids = data.get(key) or []
        if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
            raise ValueError(f"'{key}' must be a list of ids")
        out[key] = [i.strip() for i in ids]
    return out


def titles_ids(path: Path) -> list:
    if not path.is_file():
        return []
    return [m.group(1) for line in path.read_text().splitlines() for m in [CANDIDATE_LINE.match(line)] if m]


def main() -> int:
    parser = M.base_parser("Merge the scouts' triage.<k>.json parts into one triage.json.")
    parser.add_argument("--merge", nargs="+", required=True, help="the triage.<k>.json files, one per scout")
    parser.add_argument("--out", help="where to write the merged file (default references/<slug>/triage.json)")
    parser.add_argument("--titles", help="candidates_titles.md to check against (default the topic's own)")
    args = parser.parse_args()
    tdir = M.topic_dir(args)
    out_path = Path(args.out) if args.out else tdir / "triage.json"
    titles_path = Path(args.titles) if args.titles else tdir / "candidates_titles.md"

    merged = {key: [] for key in TRIAGE_KEYS}
    where = {}                                  # id -> (part file, key), to name a double listing
    clashes = []
    for name in args.merge:
        path = Path(name)
        try:
            part = load_part(path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            M.log(f"triage: cannot read {path}: {e}")
            return M.EXIT_USAGE
        for key in TRIAGE_KEYS:
            for pid in part[key]:
                if pid in where:
                    clashes.append(f"{pid} ({where[pid][0]}:{where[pid][1]} and {path.name}:{key})")
                    continue
                where[pid] = (path.name, key)
                merged[key].append(pid)
    if clashes:
        M.log(f"triage: {len(clashes)} id(s) listed twice: {'; '.join(clashes[:10])}")
        return M.EXIT_USAGE
    untriaged = [pid for pid in titles_ids(titles_path) if pid not in where]
    if untriaged:
        M.log(f"triage: {len(untriaged)} id(s) in {titles_path.name} that no part triaged: {', '.join(untriaged[:10])}")
        return M.EXIT_USAGE

    out_path.write_text(json.dumps(merged, indent=1) + "\n")
    print(f"triage: keep={len(merged['keep'])} drop={len(merged['drop'])} undecided={len(merged['undecided'])} "
          f"parts={len(args.merge)} written={out_path}")
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
