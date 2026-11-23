# SPDX-License-Identifier: Apache-2.0
import pytest

from feederline_mcp import config


def test_local_adapter_is_registered():
    assert "local" in config.installed_adapters()
    from feederline_mcp.adapters import local

    assert config.load_adapter("local") is local.build


def test_unknown_adapter_lists_installed():
    with pytest.raises(RuntimeError, match=r"unknown adapter 'nope'.*installed adapters: .*local"):
        config.load_adapter("nope")


def test_default_adapter_is_local(monkeypatch):
    monkeypatch.delenv("FEEDERLINE_MCP_ADAPTER", raising=False)
    assert config.Settings().adapter == "local"
