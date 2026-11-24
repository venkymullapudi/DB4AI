"""Maintainer eval set: runs in memory, no database (these tests don't use the `world` fixture)."""

import json

import pytest

from db4ai import evals
from db4ai.cli import main
from db4ai.embeddings import HashEmbedder
from db4ai.llm import Contradiction, HeuristicLLM


def test_builtin_set_is_well_formed():
    cases = evals.load_cases()
    assert len({c["id"] for c in cases}) == len(cases)
    for task in evals.TASKS:
        labels = {c["expected"] for c in cases if c["task"] == task}
        assert labels == {True, False}, f"{task} needs positive and negative cases"


def test_heuristic_baseline_does_not_regress():
    r = evals.run(evals.load_cases(), HeuristicLLM(), HashEmbedder(), dup_threshold=0.92)
    dedup, stale = r["tasks"]["dedup"], r["tasks"]["staleness"]
    # Floors pinned at the current baseline; raise them when the offline maintainers improve.
    assert dedup["precision"] >= 1.0 and dedup["recall"] >= 0.83
    assert stale["precision"] >= 1.0 and stale["recall"] >= 0.62
    missed = {m["id"] for m in r["misses"]}
    assert "stale-owner" in missed  # no digits: the heuristic can't see it
    assert not missed & {"stale-other-subject", "stale-other-customer", "stale-month-moved"}


@pytest.mark.parametrize("old, new, expected", [
    ("The Atlas launch date is 2026-03-01.", "The Hermes launch date is 2026-06-10.", False),  # other subject
    ("The Atlas launch date is 2026-03-01.", "The Atlas launch date is 2026-06-10.", True),  # shared subject
    ("The launch date is 2026-03-01.", "The Hermes launch date is 2026-06-10.", True),  # only one side names one
    ("The Atlas beta opens on March 3.", "The Atlas beta now opens on April 14.", True),  # months aren't subjects
    ("The launch is on March 3.", "The launch is now on April 14.", True),
])
def test_heuristic_contradiction_needs_a_shared_subject(old, new, expected):
    assert HeuristicLLM().contradiction(old, new).contradicts is expected


def test_heuristic_entities_drop_only_the_sentence_start_word():
    e = HeuristicLLM().enrich("Atlas launch date", "The Atlas launch moved. We told Globex Corp on Friday.").entities
    assert "Atlas" in e and "Globex Corp" in e and not any(x.startswith(("The ", "We ")) for x in e)


def test_metrics_and_relate_rules_are_applied():
    item = {"type": "memory", "title": "T", "body": "same words here"}
    cases = [
        {"id": "a", "task": "dedup", "old": item, "new": dict(item), "expected": True},
        {"id": "b", "task": "dedup", "old": item, "new": {**item, "type": "document"}, "expected": True},
        # a duplicate pair is never checked for staleness, as in relate
        {"id": "c", "task": "staleness", "old": item, "new": dict(item), "expected": False},
        {"id": "d", "task": "staleness", "old": item, "new": dict(item), "expected": True, "old_is_older": False},
    ]

    class Always:
        name = "always"
        calls = 0

        def contradiction(self, old, new):
            Always.calls += 1
            return Contradiction(contradicts=True, explanation="x", confidence=1.0)

    r = evals.run(cases, Always(), HashEmbedder(), dup_threshold=0.92)
    assert r["tasks"]["dedup"] == {"cases": 2, "tp": 1, "fp": 0, "fn": 1, "tn": 0,
                                   "precision": 1.0, "recall": 0.5, "f1": 0.667}
    assert r["tasks"]["staleness"]["fn"] == 1 and r["tasks"]["staleness"]["tn"] == 1
    assert Always.calls == 0  # duplicate, and old not older: the LLM is never asked
    assert [m["id"] for m in r["misses"]] == ["b", "d"]


@pytest.mark.parametrize("bad", [
    {"cases": []},
    {"cases": [{"id": "x", "task": "linking", "old": {}, "new": {}, "expected": True}]},
    {"cases": [{"id": "x", "task": "dedup", "old": {"type": "memory", "title": "t"},
                "new": {"type": "memory", "title": "t", "body": "b"}, "expected": True}]},
    {"cases": [{"id": "x", "task": "dedup", "old": {"type": "m", "title": "t", "body": "b"},
                "new": {"type": "m", "title": "t", "body": "b"}, "expected": "yes"}]},
])
def test_malformed_files_are_rejected(tmp_path, bad):
    p = tmp_path / "cases.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        evals.load_cases(p)


def test_cli_eval_gates_on_precision(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DB4AI_EMBEDDER", "hash")
    main(["eval", "--llm", "heuristic"])
    out = json.loads(capsys.readouterr().out)
    assert out["llm"] == "heuristic" and set(out["tasks"]) == {"dedup", "staleness"}
    main(["eval", "--llm", "heuristic", "--min-precision", "0.5"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as e:
        main(["eval", "--llm", "heuristic", "--dup-threshold", "0.3", "--min-precision", "0.99"])
    assert e.value.code == 1
    # a low threshold merges the hard negatives: the gate catches it
    with pytest.raises(SystemExit):
        main(["eval", "--llm", "heuristic", "--dup-threshold", "0.3", "--min-precision", "0.9"])
    with pytest.raises(SystemExit) as e:
        main(["eval", "--cases", str(tmp_path / "missing.json")])
    assert "bad eval file" in str(e.value.code)
