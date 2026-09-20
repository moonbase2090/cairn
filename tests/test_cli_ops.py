"""CLI ops tests — drive main() with an isolated CAIRN_DIR."""
import json

from cairn.cli import main


def run(argv, capsys):
    rc = main(argv)
    out, err = capsys.readouterr()
    return rc, out, err


def test_whoami_doctor_log_purge_guard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "cli-test")
    assert run(["init"], capsys)[0] == 0
    rc, out, _ = run(["whoami", "--json"], capsys)
    assert rc == 0 and json.loads(out)["agent"] == "cli-test"

    rc, out, _ = run(["doctor", "--json"], capsys)
    assert rc == 0 and json.loads(out)["memories"] == 0

    assert run(["store", "ops memory one two three", "--team", "t", "--task", "k"], capsys)[0] == 0
    rc, out, _ = run(["log", "--json"], capsys)
    assert rc == 0
    assert any(line["action"] == "store" for line in json.loads(out))

    rc, _, err = run(["purge", "k-abc"], capsys)
    assert rc == 2 and "--force" in err  # destructive guard

    assert run(["retrieve", "ops memory", "--team", "t", "--top-k", "3"], capsys)[0] == 0
    assert run(["list", "--task", "k", "--limit", "5"], capsys)[0] == 0


def test_version_flag(capsys):
    import cairn
    with __import__("pytest").raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out, _ = capsys.readouterr()
    assert cairn.__version__ in out and out.startswith("cairn ")
