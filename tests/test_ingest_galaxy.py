"""Ingest + galaxy tests — offline, tmp dirs."""
from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.galaxy import galaxy, galaxy_html, to_points
from cairn.ingest import chunk_markdown, ingest_dir
from cairn.store import Vault


def make_client(db_path, agent="ingest-bot"):
    emb = HashEmbedder()
    return CairnClient(Vault(db_path, emb.name, emb.dims, create=True), agent, emb)


def test_chunk_markdown_sections():
    chunks = chunk_markdown("# Title\n\nintro body\n\n## Setup\n\nsetup body\n\n## Usage\n\nuse body\n")
    assert len(chunks) == 3
    assert chunks[0][0] == "Title"
    assert "setup body" in chunks[1][1]


def test_ingest_embeds_in_batches(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n\nFirst section text here.\n\n## Install\n\nInstall steps here.\n")
    (docs / "notes.txt").write_text("Plain text note about coolant manifolds.\n")
    client = make_client(tmp_path / "vault.db")
    sizes: list[int] = []
    real = client.embedder.embed

    def wrapped(texts):
        sizes.append(len(texts))
        return real(texts)

    client.embedder.embed = wrapped
    ingest_dir(client, "acme", docs)
    # one embed() per file, not per chunk (2 md sections + 1 txt = 3 texts, 2 calls)
    assert sum(sizes) == 3
    assert len(sizes) == 2
    assert sizes == [2, 1]


def test_ingest_idempotent(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n\nFirst section text here.\n\n## Install\n\nInstall steps here.\n")
    (docs / "notes.txt").write_text("Plain text note about coolant manifolds.\n")
    client = make_client(tmp_path / "vault.db")
    first = ingest_dir(client, "acme", docs)
    assert first["files"] == 2
    assert first["created"] == 3  # 2 sections + 1 txt
    assert first["flagged"] == 0
    second = ingest_dir(client, "acme", docs)
    assert second["created"] == 0 and second["unchanged"] == 3
    hits = client.retrieve_memory("install steps", filters={"task_id": "guide"})
    assert hits and "Install steps" in (hits[0].content or "")


def test_galaxy_renders_vault(tmp_path):
    client = make_client(tmp_path / "vault.db", "claude-g")
    emb = HashEmbedder()
    from cairn.store import Vault as V

    other = CairnClient(V(tmp_path / "vault.db", emb.name, emb.dims), "grok-g", emb)
    for i in range(4):
        client.store_memory(f"planner memory number {i} about manifolds", team_id="t", task_id="k")
    for i in range(2):
        other.store_memory(f"researcher memory number {i} about valves", team_id="t", task_id="k")
    out = tmp_path / "galaxy.html"
    res = galaxy(client, out)
    assert res["memories"] == 6
    text = out.read_text()
    assert '<canvas id="space"' in text
    assert "Cairn" in text and "claude-g" in text and "grok-g" in text


def test_galaxy_http_serves_page(tmp_path):
    from urllib.request import urlopen

    from cairn.galaxy import start_background

    client = make_client(tmp_path / "vault.db", "claude-g")
    client.store_memory("planner memory about manifolds in the galaxy", team_id="t", task_id="k")
    srv = start_background(client, host="127.0.0.1", port=0)
    try:
        host, port = srv.server_address[:2]
        with urlopen(f"http://{host}:{port}/", timeout=5) as resp:
            text = resp.read().decode()
        assert resp.status == 200
        assert '<canvas id="space"' in text and "claude-g" in text and "manifolds" in text
        with urlopen(f"http://{host}:{port}/health", timeout=5) as health:
            assert b"ok" in health.read()
    finally:
        srv.shutdown()
        srv.server_close()


def test_galaxy_empty_vault(tmp_path):
    client = make_client(tmp_path / "vault.db")
    res = galaxy(client, tmp_path / "g.html")
    assert res["memories"] == 0
    assert (tmp_path / "g.html").exists()
    assert '<canvas id="space"' in (tmp_path / "g.html").read_text()


def test_galaxy_points_carry_pca3_depth(tmp_path):
    client = make_client(tmp_path / "vault.db")
    for i in range(5):
        client.store_memory(f"depth probe memory {i} about ion thrusters", team_id="t", task_id="k")
    pts = to_points(client)
    assert len(pts) == 5
    assert all("z" in p for p in pts)
    assert all(-1.0 <= p["z"] <= 1.0 for p in pts)
    assert len({p["z"] for p in pts}) > 1  # real spread, not flat


def test_galaxy_points_few_memories_still_have_z(tmp_path):
    client = make_client(tmp_path / "vault.db")
    client.store_memory("lone memory about sails", team_id="t", task_id="k")
    client.store_memory("second memory about sails", team_id="t", task_id="k")
    pts = to_points(client)
    assert len(pts) == 2
    assert all(p["z"] == 0 for p in pts)  # <3 points: x/y only, z falls back to 0


def test_galaxy_template_has_warp_mode(tmp_path):
    client = make_client(tmp_path / "vault.db")
    client.store_memory("warp probe memory about sails", team_id="t", task_id="k")
    html, _ = galaxy_html(client)
    assert "__DATA__" not in html  # placeholder consumed
    for hook in ('id="mode3d"', 'id="gl"', 'id="warp"', 'id="labels"', 'id="halo"',
                 "getContext('webgl'", 'warp3d', 'flyCluster3', 'draw3', 'project3'):
        assert hook in html, hook


def test_galaxy_teams_get_separate_islands(tmp_path):
    client = make_client(tmp_path / "vault.db", "a1")
    other = make_client(tmp_path / "vault.db", "a2")
    for i in range(5):
        client.store_memory(f"alpha memory {i} about coolant manifolds pressure", team_id="alpha", task_id="n")
        other.store_memory(f"bravo memory {i} about ion thrusters plasma", team_id="bravo", task_id="p")
    pts = to_points(client)
    assert len(pts) == 10
    assert all(-1.0 <= p["x"] <= 1.0 and -1.0 <= p["y"] <= 1.0 and -1.0 <= p["z"] <= 1.0 for p in pts)
    groups = {}
    for p in pts:
        groups.setdefault(p["team"], []).append((p["x"], p["y"]))
    assert set(groups) == {"alpha", "bravo"}
    cents = {t: (sum(x for x, _ in g) / len(g), sum(y for _, y in g) / len(g)) for t, g in groups.items()}
    spreads = {t: max(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for x, y in groups[t])
               for t, (cx, cy) in cents.items()}
    (ax, ay), (bx, by) = cents["alpha"], cents["bravo"]
    inter = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
    assert inter > max(spreads.values())  # islands separated, not piled up


def test_galaxy_single_team_layout_unchanged(tmp_path):
    client = make_client(tmp_path / "vault.db")
    for i in range(5):
        client.store_memory(f"solo memory {i} about sails rigging knots", team_id="solo", task_id="k")
    pts = to_points(client)
    assert max(abs(p["x"]) for p in pts) == 1.0  # full-bleed PCA, no island shrink
    assert max(abs(p["y"]) for p in pts) == 1.0


def test_galaxy_template_has_islands_and_flat_escape(tmp_path):
    client = make_client(tmp_path / "vault.db")
    client.store_memory("isle probe memory about sails", team_id="t", task_id="k")
    html, _ = galaxy_html(client)
    for hook in ("isles", "multiIsle", "culled", "isleTags", "?flat", "flat' : '"):
        assert hook in html, hook


def test_galaxy_template_has_results_table(tmp_path):
    client = make_client(tmp_path / "vault.db")
    client.store_memory("results probe memory about sails", team_id="t", task_id="k")
    html, _ = galaxy_html(client)
    for hook in ('id="mres"', 'id="mres-meta"', 'id="backbtn"', 'id="mres-wrap"',
                 "renderResults", "openRes", "RES_CAP", "'<mark>'",
                 "drawerMode", "results table"):
        assert hook in html, hook


def test_galaxy_alive_probe(tmp_path):
    from cairn.galaxy import galaxy_alive, start_background

    assert galaxy_alive("127.0.0.1", 1, timeout=0.2) is False  # nothing lives here
    client = make_client(tmp_path / "vault.db")
    srv = start_background(client, host="127.0.0.1", port=0)
    try:
        port = srv.server_address[1]
        assert galaxy_alive("127.0.0.1", port) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_galaxy_reuses_running_server(tmp_path, monkeypatch, capsys):
    import webbrowser

    from cairn.cli import main
    from cairn.galaxy import start_background

    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "reuse-test")
    assert main(["init", "--yes", "--embed-spec", "hash"]) == 0
    capsys.readouterr()
    bg = make_client(tmp_path / ".cairn" / "vault.db", "reuse-test")
    srv = start_background(bg, host="127.0.0.1", port=0)
    try:
        port = srv.server_address[1]
        opened = []
        monkeypatch.setattr(webbrowser, "open", opened.append)
        rc = main(["galaxy", "--port", str(port)])
        out, _ = capsys.readouterr()
        assert rc == 0
        assert opened == [f"http://127.0.0.1:{port}/"]
        assert "already running" in out
    finally:
        srv.shutdown()
        srv.server_close()


def test_galaxy_reuse_honors_no_open(tmp_path, monkeypatch, capsys):
    import webbrowser

    from cairn.cli import main
    from cairn.galaxy import start_background

    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "reuse-test")
    assert main(["init", "--yes", "--embed-spec", "hash"]) == 0
    capsys.readouterr()
    bg = make_client(tmp_path / ".cairn" / "vault.db", "reuse-test")
    srv = start_background(bg, host="127.0.0.1", port=0)
    try:
        opened = []
        monkeypatch.setattr(webbrowser, "open", opened.append)
        assert main(["galaxy", "--port", str(srv.server_address[1]), "--no-open"]) == 0
        capsys.readouterr()
        assert opened == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_galaxy_busy_foreign_port_errors_cleanly(tmp_path, monkeypatch, capsys):
    import socket

    from cairn.cli import main

    monkeypatch.setenv("CAIRN_DIR", str(tmp_path / ".cairn"))
    monkeypatch.setenv("CAIRN_AGENT", "reuse-test")
    assert main(["init", "--yes", "--embed-spec", "hash"]) == 0
    capsys.readouterr()
    squat = socket.socket()
    squat.bind(("127.0.0.1", 0))
    squat.listen(1)
    try:
        rc = main(["galaxy", "--port", str(squat.getsockname()[1]), "--no-open"])
        _, err = capsys.readouterr()
        assert rc == 1
        assert "not a cairn galaxy" in err
    finally:
        squat.close()


def test_galaxy_search_endpoint(tmp_path):
    from urllib.parse import quote
    from urllib.request import urlopen

    from cairn.galaxy import start_background

    client = make_client(tmp_path / "vault.db")
    client.store_memory("the socket daemon binds here", team_id="t", task_id="k")
    client.store_memory("socket socket socket everywhere", team_id="t", task_id="k")
    client.store_memory("unrelated note about manifolds", team_id="t", task_id="k")
    srv = start_background(client, host="127.0.0.1", port=0)
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def get(path):
            import json

            with urlopen(base + path, timeout=5) as r:
                return r.status, json.loads(r.read().decode())

        status, body = get("/search?q=" + quote("socket daemon"))
        assert status == 200 and len(body["keys"]) == 1
        status, body = get("/search?q=socket")
        assert status == 200 and len(body["keys"]) == 2
        assert "everywhere" in client.get_memory(body["keys"][0]).content  # tf=3 first
        status, body = get("/search?q=socket&limit=1")
        assert len(body["keys"]) == 1
        status, body = get("/search?q=")
        assert body == {"q": "", "keys": []}
        status, body = get("/search?q=" + quote('" * ('))
        assert status == 200 and body["keys"] == []
    finally:
        srv.shutdown()
        srv.server_close()


def test_galaxy_memory_endpoint(tmp_path):
    from pathlib import Path
    from urllib.parse import quote
    from urllib.request import urlopen

    from cairn.client import CairnClient
    from cairn.embed import HashEmbedder
    from cairn.galaxy import start_background
    from cairn.store import Vault

    db = tmp_path / "vault.db"
    emb = HashEmbedder()
    client = CairnClient(Vault(db, emb.name, emb.dims, create=True), "md-test", emb,
                         audit_path=Path(tmp_path) / "audit.jsonl")
    v1 = client.store_memory("# Title\n\nbody **bold**", team_id="t", task_id="k")
    client.store_memory("## V2\n\n- item", team_id="t", task_id="k", supersedes_key=v1.key)
    srv = start_background(client, host="127.0.0.1", port=0)
    try:
        import json

        base = f"http://127.0.0.1:{srv.server_address[1]}"
        with urlopen(base + "/memory?key=" + quote(v1.key), timeout=5) as r:
            body = json.loads(r.read().decode())
        assert r.status == 200
        assert body["record"]["content"].startswith("# Title")  # markdown verbatim
        assert [(v["version"], v["status"]) for v in body["versions"]] == [(1, "superseded"), (2, "active")]
        actions = [a["action"] for a in body["audit"]]
        assert actions.count("store") == 2  # v1 create + v2 supersede linked via `supersedes`
        from urllib.error import HTTPError

        try:
            urlopen(base + "/memory?key=nope", timeout=5)
            raise AssertionError("expected 404")
        except HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_galaxy_template_has_markdown_and_expand(tmp_path):
    client = make_client(tmp_path / "vault.db")
    client.store_memory("expand probe", team_id="t", task_id="k")
    html, _ = galaxy_html(client)
    for hook in ('id="expand"', 'id="expandbtn"', "openExpand", "closeExpand",
                 "function md(", "mdInline", "mdCompact", "markTerms",
                 "xvers", "xaudit", "memory?key=", "xcopy"):
        assert hook in html, hook
