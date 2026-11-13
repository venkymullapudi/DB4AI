import json

from db4ai.cli import main


def run(capsys, *args):
    main(list(args))
    return json.loads(capsys.readouterr().out)


def test_cli_end_to_end(db_url, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DB4AI_DATABASE_URL", db_url)
    monkeypatch.setenv("DB4AI_BLOB_URL", f"file://{tmp_path}/blobs")
    monkeypatch.setenv("DB4AI_LLM", "heuristic")
    assert run(capsys, "migrate") == {"applied": []}
    user = run(capsys, "user", "dana", "--team", "eng:owner")
    assert user["token"].startswith("db4ai_")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "a.md").write_text("# Pricing\nEnterprise pricing changes in Q3.")
    out = run(capsys, "import", str(tmp_path / "notes"), "--team", "eng", "--as", "dana", "--vault")
    assert out["files"] == 1 and out["changed"] == 1
    jobs = run(capsys, "maintain", "--once")
    assert {j["kind"] for j in jobs} >= {"enrich", "relate", "hygiene", "schema_scan"}
    assert not any("error" in j for j in jobs)
