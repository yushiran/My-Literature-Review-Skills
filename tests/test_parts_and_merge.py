"""Splitting the scout's reading into parts, and merging what the parts decided.

One scout over 200 titles ran for more than forty minutes on 2026-09-14. rank.py --parts K writes
K files for K scouts, triage.py --merge joins their verdicts and checks the one-list contract the
readers rely on, and select.py takes the K selected files at once.
"""
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

import manifest as M
import rank as R
import search as S
import triage as T

_spec = importlib.util.spec_from_file_location("scripts.select", Path(__file__).resolve().parents[1] / "scripts" / "select.py")
SEL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SEL)

IDS = [f"2025-a-p{i}" for i in range(7)]


def lib(args):
    m = M.load(args)
    for i, pid in enumerate(IDS):
        m["papers"][pid] = M.new_paper(pid, title=f"Paper {i}", venue="ICML", year=2025, citations=i, abstract="an abstract")
    M.save(args, m)
    return M.topic_dir(args)


def run(module, name, args, argv):
    sys.argv = [name, "--topic", args.topic, "--root", args.root] + argv
    return module.main()


def ids_in(path):
    return [line.split(". ", 1)[1].strip() for line in path.read_text().splitlines() if line.startswith("## ")]


# ---------------------------------------------------------------- rank --parts

def test_parts_partition_the_titles_and_remove_stale_parts(args):
    d = lib(args)
    (d / "candidates_titles.4.md").write_text("## 1. stale\n")     # left by an earlier run with more parts
    assert run(R, "rank.py", args, ["--parts", "3"]) == 0
    parts = [ids_in(d / f"candidates_titles.{k}.md") for k in (1, 2, 3)]
    assert sorted(sum(parts, [])) == sorted(IDS)                 # every id in exactly one part
    assert all(parts) and max(map(len, parts)) - min(map(len, parts)) <= 1   # round-robin, balanced
    assert not (d / "candidates_titles.4.md").exists()
    assert sorted(ids_in(d / "candidates_titles.md")) == sorted(IDS)   # the full file is still written


def test_parts_under_only_split_the_abstract_file_not_the_titles(args):
    d = lib(args)
    assert run(R, "rank.py", args, ["--parts", "2"]) == 0
    before = (d / "candidates_titles.1.md").read_text()
    kept = IDS[:5]
    (d / "triage.json").write_text(json.dumps({"keep": kept[:2], "drop": IDS[5:], "undecided": kept[2:]}))
    assert run(R, "rank.py", args, ["--only", str(d / "triage.json"), "--parts", "2"]) == 0
    parts = [ids_in(d / f"candidates.{k}.md") for k in (1, 2)]
    assert sorted(sum(parts, [])) == sorted(kept)
    assert (d / "candidates_titles.1.md").read_text() == before   # the scouts have read the titles parts already


def test_one_part_writes_no_part_files(args):
    d = lib(args)
    assert run(R, "rank.py", args, []) == 0
    assert not list(d.glob("candidates_titles.[0-9]*.md"))


# ---------------------------------------------------------------- triage --merge

def titles(d, ids):
    (d / "candidates_titles.md").write_text("".join(f"## {i}. {pid}\n" for i, pid in enumerate(ids, 1)))


def test_merge_joins_the_parts_into_one_file(args, capsys):
    d = lib(args)
    titles(d, IDS)
    (d / "triage.1.json").write_text(json.dumps({"keep": IDS[:1], "drop": IDS[1:3], "undecided": [IDS[3]]}))
    (d / "triage.2.json").write_text(json.dumps({"keep": [IDS[4]], "drop": IDS[5:], "undecided": []}))
    assert run(T, "triage.py", args, ["--merge", str(d / "triage.1.json"), str(d / "triage.2.json")]) == 0
    merged = json.loads((d / "triage.json").read_text())
    assert merged == {"keep": [IDS[0], IDS[4]], "drop": IDS[1:3] + IDS[5:], "undecided": [IDS[3]]}
    assert "keep=2 drop=4 undecided=1 parts=2" in capsys.readouterr().out


def test_merge_refuses_an_id_in_two_lists(args):
    d = lib(args)
    titles(d, IDS[:2])
    (d / "triage.1.json").write_text(json.dumps({"keep": [IDS[0]], "drop": [], "undecided": []}))
    (d / "triage.2.json").write_text(json.dumps({"keep": [IDS[1]], "drop": [IDS[0]], "undecided": []}))
    assert run(T, "triage.py", args, ["--merge", str(d / "triage.1.json"), str(d / "triage.2.json")]) == M.EXIT_USAGE
    assert not (d / "triage.json").exists()


def test_merge_refuses_a_title_no_part_triaged(args):
    """The scout's contract is every id in exactly one list; a scout that stopped early breaks it."""
    d = lib(args)
    titles(d, IDS[:3])
    (d / "triage.1.json").write_text(json.dumps({"keep": [IDS[0]], "drop": [IDS[1]], "undecided": []}))
    assert run(T, "triage.py", args, ["--merge", str(d / "triage.1.json")]) == M.EXIT_USAGE


def test_merge_refuses_a_renamed_key(args):
    d = lib(args)
    (d / "triage.1.json").write_text(json.dumps({"kept": [IDS[0]], "dropped": [IDS[1]]}))
    assert run(T, "triage.py", args, ["--merge", str(d / "triage.1.json")]) == M.EXIT_USAGE


# ---------------------------------------------------------------- select --file a b

def test_select_applies_several_files_at_once(args):
    d = lib(args)
    (d / "candidates.md").write_text("".join(f"## {i}. {pid}\n" for i, pid in enumerate(IDS, 1)))
    (d / "selected.1.json").write_text(json.dumps([{"id": IDS[0], "why": "part one"}]))
    (d / "selected.2.json").write_text(json.dumps([{"id": IDS[1], "why": "part two"}, {"id": IDS[0], "why": "again"}]))
    assert run(SEL, "select.py", args, ["--file", str(d / "selected.1.json"), str(d / "selected.2.json"), "--target", "5"]) == 0
    papers = M.load(args)["papers"]
    assert papers[IDS[0]]["status"] == "selected" and papers[IDS[0]]["why"] == "part one"   # first file wins
    assert papers[IDS[1]]["status"] == "selected"
    assert all(papers[pid]["status"] == "rejected" for pid in IDS[2:])


# ---------------------------------------------------------------- seed title with a colon

def test_seed_title_with_a_colon_builds_a_filter_openalex_accepts(args, monkeypatch):
    """OpenAlex read the subtitle colon as filter syntax and answered HTTP 400 (2026-09-14)."""
    urls = []
    work = {"id": "https://openalex.org/W1", "doi": "", "ids": {}, "title": "Reasons for the Superiority: Robustness",
            "publication_year": 2023, "cited_by_count": 10, "authorships": [{"author": {"display_name": "G. Ohayon"}}],
            "primary_location": {"source": {"display_name": "ICML"}}, "best_oa_location": None, "locations": [],
            "abstract_inverted_index": {"We": [0]}, "relevance_score": None, "referenced_works": []}

    def fake_get(url, headers=None, name="x"):
        urls.append(url)
        return {"results": [work]}

    monkeypatch.setattr(S, "get_json_retry", fake_get)
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root, "--seed", "Reasons for the Superiority: Robustness, Consistency"]
    try:
        S.main()
    except SystemExit as e:
        assert e.code == 0
    (url,) = [u for u in urls if "title.search" in u]
    value = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["filter"][0].split(":", 1)[1]
    assert not set(value) & set(":|,")
    assert M.load(args)["papers"] and next(iter(M.load(args)["papers"].values()))["status"] == "selected"
