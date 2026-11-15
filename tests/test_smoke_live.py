"""Opt-in live smoke against local Ollama. Skipped by default.

The Founder's laptop (i5-5300U, 12 GB) cannot run live 6.7B missions without
freezing, so live verification is manual-only:

    set FORGE_LIVE_SMOKE=1
    .venv\\Scripts\\python -m pytest tests/test_smoke_live.py -v

Or run a full manual mission (starts Ollama itself):

    .venv\\Scripts\\python -m forge.cli chief run --task SMOKE-2

This test only verifies the live adapter round-trip (cheap, one short call);
full missions are the CLI's job. Models must already exist; nothing is
downloaded.
"""
from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FORGE_LIVE_SMOKE") != "1",
        reason="live Ollama smoke is opt-in (FORGE_LIVE_SMOKE=1); "
               "default CI/laptop runs use the FakeModel suite instead",
    ),
]


def test_live_ollama_adapter_round_trip() -> None:
    from forge.model.ollama_backend import OllamaBackend

    backend = OllamaBackend("deepseek-coder:6.7b")
    reply = backend.chat(
        [
            {"role": "system", "content": "Reply with exactly one JSON object."},
            {"role": "user", "content": 'Return {"ok": true}'},
        ],
        format="json",
    )
    assert '"ok"' in reply
