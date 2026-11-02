"""The LLM interface is exercised with a fake transport; no test opens a network socket."""
from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stock_gplm.llm import BudgetExceeded, LLMClient, LLMDisabled, UploadRefused, load_config, redact

REPO = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path, **limits) -> Path:
    raw = json.loads((REPO / "config/llm/providers.example.json").read_text())
    raw["enabled"] = True
    raw["audit_log"] = str(tmp_path / "audit.jsonl")
    raw["limits"] |= limits
    for item in raw["providers"].values():
        item["enabled"] = True
    path = tmp_path / "llm.json"
    path.write_text(json.dumps(raw))
    return path


class Fake:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), body))
        if "/api/chat" in url:
            return {"message": {"content": "local reply"}, "prompt_eval_count": 10, "eval_count": 5}
        return {"choices": [{"message": {"content": "cloud reply"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


def _client(tmp_path, transport, env=None, **limits):
    clock = lambda: datetime(2026, 11, 2, 10, tzinfo=timezone.utc)
    return LLMClient(load_config(_config(tmp_path, **limits)), transport=transport,
                     environ=env if env is not None else {"OPENROUTER_API_KEY": "or-abcdefghijklmnopqrstuvwxyz"},
                     clock=clock)


def test_shipped_example_is_disabled_and_refuses_every_call(tmp_path):
    config = load_config(REPO / "config/llm/providers.example.json")
    config.audit_log = tmp_path / "a.jsonl"
    fake = Fake()
    with pytest.raises(LLMDisabled):
        LLMClient(config, transport=fake).complete(provider="ollama", model="qwen2.5:14b-instruct",
                                                   purpose="t", prompt="hi")
    assert fake.calls == []


def test_keys_come_only_from_the_environment(tmp_path):
    raw = json.loads(_config(tmp_path).read_text())
    raw["providers"]["openrouter"]["api_key"] = "x"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="environment"):
        load_config(path)
    with pytest.raises(LLMDisabled, match="OPENROUTER_API_KEY"):
        _client(tmp_path, Fake(), env={}).complete(provider="openrouter", model="example/approved-model",
                                                   purpose="t", prompt="hi")


def test_secrets_and_paths_are_redacted_before_sending_and_audit_holds_no_prompt(tmp_path):
    fake = Fake()
    secret = "or-abcdefghijklmnopqrstuvwxyz0123"
    reply = _client(tmp_path, fake).complete(
        provider="openrouter", model="example/approved-model", purpose="feature ideas",
        prompt=f"key {secret} SHOONYA_PASSWORD=hunter2 mail a@b.com file /home/u/data/raw/x.gz")
    sent = fake.calls[0][2]["messages"][0]["content"]
    assert secret not in sent and "hunter2" not in sent and "a@b.com" not in sent and "/home/u" not in sent
    assert reply.authority == "ADVISORY_ONLY" and reply.text == "cloud reply"
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "hunter2" not in audit and "feature ideas" in audit and "prompt_sha256" in audit
    assert fake.calls[0][1]["Authorization"].startswith("Bearer ")


def test_market_rows_cannot_go_to_a_remote_provider_without_approval_but_may_stay_local(tmp_path):
    rows = "\n".join(f"RELIANCE-EQ,2026-10-{d:02d},1,2,3,4,5" for d in range(1, 6))
    fake = Fake()
    client = _client(tmp_path, fake)
    with pytest.raises(UploadRefused):
        client.complete(provider="openrouter", model="example/approved-model", purpose="t", prompt=rows)
    assert fake.calls == []
    client.complete(provider="ollama", model="qwen2.5:14b-instruct", purpose="t", prompt=rows)
    client.complete(provider="openrouter", model="example/approved-model", purpose="t", prompt=rows,
                    approved_data_upload=True)
    assert len(fake.calls) == 2


def test_token_and_spend_limits_are_enforced_before_sending(tmp_path):
    fake = Fake()
    client = _client(tmp_path, fake, daily_token_limit=1010, max_output_tokens=1000)
    client.complete(provider="ollama", model="qwen2.5:14b-instruct", purpose="t", prompt="hello")
    with pytest.raises(BudgetExceeded):
        client.complete(provider="ollama", model="qwen2.5:14b-instruct", purpose="t", prompt="hello")
    (tmp_path / "s").mkdir()
    poor = _client(tmp_path / "s", Fake(), daily_usd_limit=0.001)
    with pytest.raises(BudgetExceeded):
        poor.complete(provider="openrouter", model="example/approved-model", purpose="t", prompt="hi")
    assert len(fake.calls) == 1


def test_unapproved_models_are_refused(tmp_path):
    with pytest.raises(LLMDisabled, match="not approved"):
        _client(tmp_path, Fake()).complete(provider="ollama", model="other", purpose="t", prompt="hi")


def test_redact_reports_counts():
    text, counts = redact("Bearer abcdefghijklmnop1234 and x@y.org")
    assert "[REDACTED_BEARER]" in text and counts == {"BEARER": 1, "EMAIL": 1}


def test_training_and_validation_code_never_imports_the_llm_package():
    for path in (REPO / "src/stock_gplm/m35").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any("llm" in name for name in names), path
