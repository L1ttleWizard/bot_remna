"""Тесты узлового слоя: API-методы для нод (моки aiohttp) + сборка ssh-команды."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Заглушки обязательных env, чтобы импорт config не падал.
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("REMNAWAVE_URL", "https://panel.example.com")
os.environ.setdefault("REMNAWAVE_TOKEN", "test-token")
os.environ.setdefault("SUB_DOMAIN", "https://sub.example.com")


class _FakeResp:
    def __init__(self, status: int, json_data=None, text_data: str = ""):
        self.status = status
        self._json = json_data
        self._text = text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text


class _FakeSession:
    """Минимальный заменитель `aiohttp.ClientSession` под наш код."""

    def __init__(self, on_request):
        self._on_request = on_request

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, **kw):
        return self._on_request("GET", url, kw)

    def post(self, url, **kw):
        return self._on_request("POST", url, kw)

    def patch(self, url, **kw):
        return self._on_request("PATCH", url, kw)

    def delete(self, url, **kw):
        return self._on_request("DELETE", url, kw)


@pytest.fixture
def api_instance():
    from remnawave_api import RemnawaveAPI
    return RemnawaveAPI("https://panel.example.com", "tkn")


def _patch_session(handler):
    """Подменяет aiohttp.ClientSession внутри remnawave_api на _FakeSession(handler)."""
    return patch("remnawave_api.aiohttp.ClientSession", lambda *a, **kw: _FakeSession(handler))


def test_list_nodes_unwraps_response(api_instance):
    def on_req(method, url, kw):
        assert method == "GET"
        assert url.endswith("/api/nodes")
        return _FakeResp(200, json_data={"response": [{"uuid": "u1", "name": "n1"}]})

    with _patch_session(on_req):
        nodes = asyncio.run(api_instance.list_nodes())
    assert nodes == [{"uuid": "u1", "name": "n1"}]


def test_list_nodes_passthrough_list(api_instance):
    def on_req(method, url, kw):
        return _FakeResp(200, json_data=[{"uuid": "u1"}])

    with _patch_session(on_req):
        nodes = asyncio.run(api_instance.list_nodes())
    assert nodes == [{"uuid": "u1"}]


def test_list_nodes_error_returns_none(api_instance):
    def on_req(method, url, kw):
        return _FakeResp(500, text_data="boom")

    with _patch_session(on_req):
        assert asyncio.run(api_instance.list_nodes()) is None


def test_get_node(api_instance):
    def on_req(method, url, kw):
        assert method == "GET"
        assert url.endswith("/api/nodes/abc")
        return _FakeResp(200, json_data={"response": {"uuid": "abc"}})

    with _patch_session(on_req):
        out = asyncio.run(api_instance.get_node("abc"))
    assert out == {"response": {"uuid": "abc"}}


@pytest.mark.parametrize("action,method,url_suffix", [
    ("enable", "enable_node", "/api/nodes/abc/actions/enable"),
    ("disable", "disable_node", "/api/nodes/abc/actions/disable"),
    ("restart", "restart_node", "/api/nodes/abc/actions/restart"),
    ("reset_traffic", "reset_node_traffic", "/api/nodes/abc/actions/reset-traffic"),
])
def test_node_actions(api_instance, action, method, url_suffix):
    seen = {}

    def on_req(http_method, url, kw):
        seen["url"] = url
        seen["http_method"] = http_method
        return _FakeResp(200, json_data={"ok": True})

    with _patch_session(on_req):
        ok = asyncio.run(getattr(api_instance, method)("abc"))
    assert ok is True
    assert seen["http_method"] == "POST"
    assert seen["url"].endswith(url_suffix)


def test_restart_all_nodes(api_instance):
    def on_req(http_method, url, kw):
        assert http_method == "POST"
        assert url.endswith("/api/nodes/actions/restart-all")
        return _FakeResp(200, json_data={"ok": True})

    with _patch_session(on_req):
        ok = asyncio.run(api_instance.restart_all_nodes())
    assert ok is True


def test_create_node_returns_json(api_instance):
    def on_req(http_method, url, kw):
        assert http_method == "POST"
        return _FakeResp(201, json_data={"response": {"uuid": "new"}})

    with _patch_session(on_req):
        out = asyncio.run(api_instance.create_node({"name": "x"}))
    assert out == {"response": {"uuid": "new"}}


def test_delete_node_204(api_instance):
    def on_req(http_method, url, kw):
        assert http_method == "DELETE"
        return _FakeResp(204)

    with _patch_session(on_req):
        ok = asyncio.run(api_instance.delete_node("abc"))
    assert ok is True


def test_build_add_node_command_quotes_args():
    from services.master_ssh import MasterSSHConfig, build_add_node_command

    cfg = MasterSSHConfig(
        host="m.example.com",
        port=22,
        user="root",
        key_path="/run/secrets/k",
        ansible_repo_path="/root/Ansible-deploy_new_node-playbook",
    )
    cmd = build_add_node_command(
        cfg,
        name="eu_node_3",
        address="1.2.3.4",
        ssh_port=22,
        node_port=3743,
        bridge_sni="www.microsoft.com",
        country_code="NL",
    )
    assert "/root/Ansible-deploy_new_node-playbook/scripts/add_node.sh" in cmd
    assert "--name eu_node_3" in cmd
    assert "--address 1.2.3.4" in cmd
    assert "--ssh-port 22" in cmd
    assert "--node-port 3743" in cmd
    assert "--bridge-sni www.microsoft.com" in cmd
    assert "--country NL" in cmd


def test_build_add_node_command_escapes_special_chars():
    from services.master_ssh import MasterSSHConfig, build_add_node_command

    cfg = MasterSSHConfig(
        host="m", port=22, user="r", key_path="/k",
        ansible_repo_path="/repo",
    )
    cmd = build_add_node_command(
        cfg,
        name="weird-name",
        address="host.with.semicolon",
        ssh_port=22, node_port=3743,
        bridge_sni="sni; rm -rf /",
        country_code="DE",
    )
    # SNI с пробелами/спецсимволами должен быть в кавычках, не вызвать инъекцию.
    assert "'sni; rm -rf /'" in cmd
    assert "rm -rf /" not in cmd.split("'sni; rm -rf /'")[0]
