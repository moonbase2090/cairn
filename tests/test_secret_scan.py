"""Secret admission runs before content reaches embeddings, storage, or audit."""
from __future__ import annotations

import json

import pytest

from cairn.client import CairnClient
from cairn.embed import HashEmbedder
from cairn.ingest import ingest_dir
from cairn.models import content_digest
from cairn.secret_scan import SecretAdmissionError, find_secret_category, scan_content
from cairn.store import Vault


OPAQUE_VALUE = "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6"


def make_client(vdir) -> CairnClient:
    embedder = HashEmbedder()
    vault = Vault(vdir / "vault.db", embedder.name, embedder.dims, create=True)
    return CairnClient(vault, "scanner-test", embedder, audit_path=vdir / "audit.jsonl")


@pytest.mark.parametrize(
    "label",
    [
        "PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "DSA PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
    ],
)
@pytest.mark.parametrize("newline", ["\n", r"\n"])
def test_private_key_blocks_are_rejected_for_literal_and_escaped_newlines(label, newline):
    content = f"-----BEGIN {label}-----{newline}not-a-real-key{newline}-----END {label}-----"
    with pytest.raises(SecretAdmissionError) as caught:
        scan_content(content)
    assert caught.value.category == "private key"
    assert "not-a-real-key" not in str(caught.value)


@pytest.mark.parametrize("prefix", ["ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_"])
def test_supported_github_prefixes_do_not_assume_a_fixed_total_length(prefix):
    suffix = "0123456789abcdefABCDEF"
    with pytest.raises(SecretAdmissionError) as caught:
        scan_content(f"token={prefix}{suffix}")
    assert caught.value.category == "GitHub token"
    assert suffix not in str(caught.value)


@pytest.mark.parametrize("prefix", ["AKIA", "ASIA"])
def test_aws_access_key_ids_are_rejected(prefix):
    with pytest.raises(SecretAdmissionError) as caught:
        scan_content(f"aws_access_key_id={prefix}1234567890ABCDEF")
    assert caught.value.category == "AWS credential"
    assert "1234567890ABCDEF" not in str(caught.value)


@pytest.mark.parametrize("name", ["aws_secret_access_key", "aws_session_token"])
def test_aws_secret_and_session_assignments_are_rejected(name):
    with pytest.raises(SecretAdmissionError) as caught:
        scan_content(f'{name} = "{OPAQUE_VALUE}{OPAQUE_VALUE}"')
    assert caught.value.category == "AWS credential"
    assert OPAQUE_VALUE not in str(caught.value)


def test_json_credential_assignments_are_rejected():
    with pytest.raises(SecretAdmissionError) as caught:
        scan_content(f'{{"aws_session_token":"{OPAQUE_VALUE}{OPAQUE_VALUE}"}}')
    assert caught.value.category == "AWS credential"


@pytest.mark.parametrize("content", [
    f'api_key="{OPAQUE_VALUE}{OPAQUE_VALUE}"',
    f"authorization: Bearer {OPAQUE_VALUE}{OPAQUE_VALUE}",
    OPAQUE_VALUE + OPAQUE_VALUE,
])
def test_generic_assignments_and_opaque_spans_are_rejected(content):
    with pytest.raises(SecretAdmissionError) as caught:
        scan_content(content)
    assert caught.value.category == "credential"
    assert OPAQUE_VALUE not in str(caught.value)


@pytest.mark.parametrize("content", [
    "Keep passwords private and never paste tokens into project notes.",
    "The API key rotation guide describes short-lived credentials.",
    "A token count of 24 helps explain the test result.",
    "UUID 550e8400-e29b-41d4-a716-446655440000 and "
    "SHA digest deadbeefdeadbeefdeadbeefdeadbeef are identifiers.",
    "The GitHub prefixes ghp_ and github_pat_ are documented formats.",
])
def test_prose_and_common_identifiers_are_not_rejected(content):
    assert find_secret_category(content) is None


def test_store_rejects_before_embedding_storage_and_audit(tmp_path, monkeypatch, caplog):
    client = make_client(tmp_path)
    monkeypatch.setattr(
        "cairn.client.content_digest",
        lambda _content: pytest.fail("secret content was hashed"),
    )
    monkeypatch.setattr(
        client.embedder, "embed",
        lambda _texts: pytest.fail("secret content reached the embedder"),
    )
    secret = f"api_key={OPAQUE_VALUE}{OPAQUE_VALUE}"

    with pytest.raises(SecretAdmissionError) as caught:
        client.store_memory(secret, team_id="team", task_id="task")

    assert OPAQUE_VALUE not in str(caught.value)
    assert client.vault.count() == 0
    assert client.vault.export_sync_events()["events"] == []
    assert not client.audit_path.exists()
    assert OPAQUE_VALUE not in caplog.text


def test_document_chunks_are_scanned_before_batch_embedding(tmp_path, monkeypatch):
    client = make_client(tmp_path / "vault")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "notes.md").write_text(
        "# Safe notes\n\nThis chunk is ordinary prose.\n\n"
        f"# Credentials\n\npassword={OPAQUE_VALUE}{OPAQUE_VALUE}\n"
    )
    monkeypatch.setattr(
        "cairn.ingest.content_digest",
        lambda _content: pytest.fail("secret document was hashed"),
    )
    monkeypatch.setattr(
        client.embedder, "embed",
        lambda _texts: pytest.fail("secret document reached batch embedding"),
    )

    with pytest.raises(SecretAdmissionError):
        ingest_dir(client, "team", docs)

    assert client.vault.count() == 0
    assert client.vault.export_sync_events()["events"] == []


def test_portable_import_scans_every_memory_before_writing(tmp_path):
    source = make_client(tmp_path / "source")
    source.store_memory("A normal exported memory.", team_id="team", task_id="task")
    source.store_memory(
        "Another normal exported memory.", team_id="team", task_id="task-2", mode="new",
    )
    pack = source.export()
    secret = f"token={OPAQUE_VALUE}{OPAQUE_VALUE}"
    pack["memories"][1]["content"] = secret
    target = make_client(tmp_path / "target")

    with pytest.raises(SecretAdmissionError) as caught:
        target.import_pack(pack)

    assert OPAQUE_VALUE not in str(caught.value)
    assert target.vault.count() == 0
    assert target.vault.export_sync_events()["events"] == []
    assert not target.audit_path.exists()


def test_sync_snapshot_import_scans_before_events_cursors_or_audit(tmp_path):
    source = make_client(tmp_path / "source")
    source.store_memory("A normal sync snapshot.", team_id="team", task_id="task")
    source.store_memory(
        "Another normal sync snapshot.", team_id="team", task_id="task-2", mode="new",
    )
    pack = source.export_delta()
    secret = f"aws_session_token={OPAQUE_VALUE}{OPAQUE_VALUE}"
    pack["events"][1]["snapshot"]["content"] = secret
    target = make_client(tmp_path / "target")
    source_identity = getattr(source.vault, "vault_identity", None)
    target_identity = getattr(target.vault, "vault_identity", None)
    if source_identity is not None and target_identity.vault_id != source_identity.vault_id:
        target.vault._set_meta("vault_id", source_identity.vault_id)
        target.vault._set_meta("vault_name", source_identity.name)
        target.vault._vault_identity = source_identity

    with pytest.raises(SecretAdmissionError) as caught:
        target.import_sync_pack(pack, peer="peer")

    assert OPAQUE_VALUE not in str(caught.value)
    assert target.vault.count() == 0
    assert target.vault.export_sync_events()["events"] == []
    assert target.vault.get_sync_cursor("peer", "pull") == 0
    assert not target.audit_path.exists()


def test_existing_vault_preflight_is_read_only_and_content_free(tmp_path):
    client = make_client(tmp_path)
    content = "AKIA1234567890ABCDEF is a pre-existing test value."
    digest = content_digest(content)
    record = {
        "key": "preflight-test-key", "canonical_id": "preflight-test",
        "content": content, "content_summary": content[:200],
        "memory_type": "semantic", "status": "active", "origin": "agent",
        "task_id": "preflight", "agent_id": "scanner-test", "team_id": "team",
        "version": 1, "created_at": 1, "updated_at": 1, "expires_at": None,
        "archived_at": None, "supersedes": None, "parent_key": None,
        "provenance": None, "confidence": None, "content_hash": f"sha256:{digest}",
    }
    vector = client.embedder.embed(["safe placeholder"])[0]
    client.vault.insert(record, vector)
    prior_events = client.vault.export_sync_events()["events"]
    client.audit_path.write_text("existing audit entry\n")
    prior_audit = client.audit_path.read_text()

    result = client.preflight_secret_scan()
    serialized = json.dumps(result)

    assert result == {"scanned": 1, "findings": {"AWS credential": 1}, "safe": False}
    assert content not in serialized
    assert digest not in serialized
    assert record["key"] not in serialized
    assert client.vault.export_sync_events()["events"] == prior_events
    assert client.audit_path.read_text() == prior_audit
