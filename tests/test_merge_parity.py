"""A paper created by two processes must end up as the paper created by one.

manifest's save-merge has its own copy of search.merge's field rules for the case
where both runs added the same id. The two drifted: every list was unioned, so a
three-author paper grew six names; relevance was never maxed; the preprint upgrade
never ran. rank.py recomputes venue_tier from the venue string on every run, so the
stale preprint venue halved the score and could drop the paper below the --top cut.
"""
import os
from types import SimpleNamespace

import manifest as M
import search as S

TITLE = "Denoising Diffusion Probabilistic Models"
PREPRINT = S.record("openalex", title=TITLE, year=2020, venue="arXiv", relevance=0.10,
                    authors=["Jonathan Ho", "Ajay Jain", "Pieter Abbeel"], citations=100)
PUBLISHED = S.record("s2", title=TITLE, year=2020, relevance=0.99,
                     venue="Advances in Neural Information Processing Systems",
                     authors=["J. Ho", "A. Jain", "P. Abbeel"], citations=100)


def only_paper(args):
    return next(iter(M.load(args)["papers"].values()))


def sequential(tmp_path, monkeypatch):
    args = SimpleNamespace(topic="t", root=str(tmp_path / "seq"))
    monkeypatch.setenv("LITREV_LOCK_DIR", str(tmp_path / "locks"))
    m = M.load(args)
    S.merge(m, [PREPRINT, PUBLISHED])
    M.save(args, m)
    return only_paper(args)


def concurrent(tmp_path, monkeypatch):
    """Two runs load, each sees one record, the published one saves first."""
    args = SimpleNamespace(topic="t", root=str(tmp_path / "par"))
    monkeypatch.setenv("LITREV_LOCK_DIR", str(tmp_path / "locks"))
    mine, theirs = M.load(args), M.load(args)
    S.merge(theirs, [PUBLISHED])
    M.save(args, theirs)
    S.merge(mine, [PREPRINT])
    M.save(args, mine)
    return only_paper(args)


def test_a_paper_created_twice_matches_a_paper_created_once(tmp_path, monkeypatch):
    one, two = sequential(tmp_path, monkeypatch), concurrent(tmp_path, monkeypatch)

    assert two["venue"] == one["venue"]          # was "arXiv": tier 4 instead of tier 1, score halved
    assert two["relevance"] == one["relevance"]  # was 0.10: rel carries 0.30 of the score bracket
    # `authors` fills only when empty, in merge and here alike, so which of the two
    # records supplies it follows arrival order in both paths. What must not happen
    # is the two being concatenated: a three-author paper came back with six names.
    assert len(two["authors"]) == len(one["authors"]) == 3
    assert two["authors"] in (PREPRINT["authors"], PUBLISHED["authors"])


def preprint_first(tmp_path, monkeypatch):
    """The order that actually exercises the max and the upgrade: the weaker record
    reaches disk first, so the later run has to improve on it rather than fill a gap."""
    args = SimpleNamespace(topic="t", root=str(tmp_path / "pre"))
    monkeypatch.setenv("LITREV_LOCK_DIR", str(tmp_path / "locks"))
    mine, theirs = M.load(args), M.load(args)
    S.merge(theirs, [PREPRINT])
    M.save(args, theirs)
    S.merge(mine, [PUBLISHED])
    M.save(args, mine)
    return only_paper(args)


def test_the_later_run_raises_relevance_instead_of_keeping_the_lower(tmp_path, monkeypatch):
    p = preprint_first(tmp_path, monkeypatch)
    assert p["relevance"] == 0.99   # a fill-only rule keeps 0.10, and rel carries 0.30 of the score


def test_a_published_record_still_upgrades_a_preprint_on_disk(tmp_path, monkeypatch):
    p = preprint_first(tmp_path, monkeypatch)
    assert p["venue"] == PUBLISHED["venue"]   # stayed "arXiv": tier 4 not tier 1, score halved


def test_the_two_runs_provenance_is_still_unioned(tmp_path, monkeypatch):
    """Restricting the union must not stop the set-valued fields accumulating."""
    p = concurrent(tmp_path, monkeypatch)
    assert sorted(p["sources"]) == ["openalex", "s2"]
    assert p["found_via"] == ["query"]


def test_authors_is_filled_when_disk_has_none(tmp_path, monkeypatch):
    """Restricting the union must not stop an empty field being filled.

    Built by hand rather than through merge, because an author-less record makes
    make_id produce a different id and the two would never meet.
    """
    args = SimpleNamespace(topic="t", root=str(tmp_path / "fill"))
    monkeypatch.setenv("LITREV_LOCK_DIR", str(tmp_path / "locks"))
    mine, theirs = M.load(args), M.load(args)
    pid = "2020-ho-denoising-diffusion-probabilistic-models"
    theirs["papers"][pid] = M.new_paper(pid, title=TITLE, year=2020, venue="arXiv", authors=[])
    M.save(args, theirs)
    S.merge(mine, [PREPRINT])
    M.save(args, mine)

    assert only_paper(args)["authors"] == PREPRINT["authors"]
