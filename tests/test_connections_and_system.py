"""Тесты для новых методов API (Connections, System Health, Digest, Recap, Bandwidth, HWID, History, GeoCheck) и UI."""
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

os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault("REMNAWAVE_URL", "https://panel.example.com")
os.environ.setdefault("REMNAWAVE_TOKEN", "test-token")
os.environ.setdefault("SUB_DOMAIN", "https://sub.example.com")

from remnawave_api import RemnawaveAPI
import keyboards as kb


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
def fake_api():
    api = RemnawaveAPI("https://panel.example.com", "dummy")
    return api


@pytest.mark.asyncio
async def test_get_user_connections_success(fake_api):
    def on_req(method, url, kw):
        if method == "POST" and "/api/connections/by-user/123" in url:
            return _FakeResp(201, {"response": {"jobId": "job-99"}})
        if method == "GET" and "/api/connections/by-user/job-99" in url:
            return _FakeResp(200, {
                "response": {
                    "isCompleted": True,
                    "result": {
                        "userId": 123,
                        "nodes": [{"nodeName": "omixochitl", "countryEmoji": "🇳🇱", "ips": ["1.2.3.4"]}]
                    }
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.get_user_connections(123)
        assert res is not None
        assert res["userId"] == 123
        assert len(res["nodes"]) == 1
        assert res["nodes"][0]["countryEmoji"] == "🇳🇱"


@pytest.mark.asyncio
async def test_get_node_connections_success(fake_api):
    node_uuid = "node-uuid-111"
    def on_req(method, url, kw):
        if method == "POST" and f"/api/connections/by-node/{node_uuid}" in url:
            return _FakeResp(201, {"response": {"jobId": "job-node-1"}})
        if method == "GET" and "/api/connections/by-node/job-node-1" in url:
            return _FakeResp(200, {
                "response": {
                    "isCompleted": True,
                    "result": {
                        "nodeUuid": node_uuid,
                        "users": [{"username": "alice", "clientIp": "10.0.0.1"}]
                    }
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.get_node_connections(node_uuid)
        assert res is not None
        assert len(res["users"]) == 1
        assert res["users"][0]["username"] == "alice"


@pytest.mark.asyncio
async def test_drop_user_connections(fake_api):
    def on_req(method, url, kw):
        if method == "POST" and "/api/connections/drop" in url:
            return _FakeResp(202, {})
        return _FakeResp(400, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        ok = await fake_api.drop_user_connections(123)
        assert ok is True


@pytest.mark.asyncio
async def test_get_system_health(fake_api):
    def on_req(method, url, kw):
        if method == "GET" and "/api/system/health" in url:
            return _FakeResp(200, {
                "response": {
                    "runtimeMetrics": [
                        {"instanceType": "api", "uptime": 1200, "rss": 100000000}
                    ]
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.get_system_health()
        assert res is not None
        assert "runtimeMetrics" in res
        assert res["runtimeMetrics"][0]["instanceType"] == "api"


@pytest.mark.asyncio
async def test_get_system_nodes_metrics(fake_api):
    def on_req(method, url, kw):
        if method == "GET" and "/api/system/nodes/metrics" in url:
            return _FakeResp(200, {
                "response": {
                    "nodes": [
                        {"nodeUuid": "uuid-1", "nodeName": "Bulgaria", "usersOnline": 5}
                    ]
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.get_system_nodes_metrics()
        assert res is not None
        assert len(res) == 1
        assert res[0]["usersOnline"] == 5


@pytest.mark.asyncio
async def test_get_system_digest(fake_api):
    def on_req(method, url, kw):
        if method == "GET" and "/api/system/stats/digest" in url:
            return _FakeResp(200, {
                "response": {
                    "users": {"createdCount": 3, "expiredCount": 1},
                    "traffic": {"totalBytes": "100000", "byUsersCreatedInRangeBytes": "50000"},
                    "hwidDevices": {"createdCount": 4}
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.get_system_digest("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
        assert res is not None
        assert res["users"]["createdCount"] == 3
        assert res["hwidDevices"]["createdCount"] == 4


@pytest.mark.asyncio
async def test_get_system_recap(fake_api):
    def on_req(method, url, kw):
        if method == "GET" and "/api/system/stats/recap" in url:
            return _FakeResp(200, {
                "response": {
                    "thisMonth": {"users": 10, "traffic": "1000"},
                    "total": {"users": 100, "nodes": 5, "distinctCountries": 4},
                    "version": "3.4.3"
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.get_system_recap()
        assert res is not None
        assert res["version"] == "3.4.3"
        assert res["total"]["nodes"] == 5


@pytest.mark.asyncio
async def test_get_system_bandwidth(fake_api):
    def on_req(method, url, kw):
        if method == "GET" and "/api/system/stats/bandwidth" in url:
            return _FakeResp(200, {
                "response": {
                    "bandwidthLastSevenDays": {"current": "100 GiB", "previous": "80 GiB"}
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.get_system_bandwidth()
        assert res is not None
        assert "bandwidthLastSevenDays" in res


@pytest.mark.asyncio
async def test_get_top_hwid_users(fake_api):
    def on_req(method, url, kw):
        if method == "GET" and "/api/hwid/devices/top-users" in url:
            return _FakeResp(200, {
                "response": {
                    "users": [{"username": "alice", "devicesCount": 10}],
                    "total": 1
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.get_top_hwid_users()
        assert res is not None
        assert len(res["users"]) == 1
        assert res["users"][0]["devicesCount"] == 10


@pytest.mark.asyncio
async def test_get_user_sub_history(fake_api):
    def on_req(method, url, kw):
        if method == "GET" and "/api/users/123/subscription-request-history" in url:
            return _FakeResp(200, {
                "response": {
                    "records": [{"requestIp": "1.1.1.1", "userAgent": "v2rayN"}]
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        records = await fake_api.get_user_sub_history(123)
        assert records is not None
        assert len(records) == 1
        assert records[0]["userAgent"] == "v2rayN"


@pytest.mark.asyncio
async def test_get_node_bandwidth_users(fake_api):
    def on_req(method, url, kw):
        if method == "GET" and "/api/bandwidth-stats/nodes/uuid-1/users" in url:
            return _FakeResp(200, {
                "response": {
                    "topUsers": [{"username": "alice", "total": 5000}]
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.get_node_bandwidth_users("uuid-1", "2026-08-01", "2026-08-02")
        assert res is not None
        assert len(res["topUsers"]) == 1


@pytest.mark.asyncio
async def test_node_geocheck(fake_api):
    def on_req(method, url, kw):
        if method == "POST" and "/api/connections/geocheck/uuid-1" in url:
            return _FakeResp(201, {"response": {"jobId": "geo-job-1"}})
        if method == "GET" and "/api/connections/geocheck/geo-job-1" in url:
            return _FakeResp(200, {
                "response": {
                    "isCompleted": True,
                    "result": {"ip": "1.2.3.4", "country": "Bulgaria", "city": "Sofia"}
                }
            })
        return _FakeResp(404, {})

    with patch.object(fake_api, "_session", return_value=_FakeSession(on_req)):
        res = await fake_api.node_geocheck("uuid-1")
        assert res is not None
        assert res["country"] == "Bulgaria"


def test_keyboards_structure():
    admin_sub_kb = kb.admin_sub_keyboard(123456, 1)
    callbacks = [btn.callback_data for row in admin_sub_kb.inline_keyboard for btn in row]
    assert "admu:123456:s:1:conn" in callbacks
    assert "admu:123456:s:1:drop_confirm" in callbacks
    assert "admu:123456:s:1:req_hist" in callbacks

    user_sub_kb = kb.user_sub_menu_keyboard(1)
    user_callbacks = [btn.callback_data for row in user_sub_kb.inline_keyboard for btn in row]
    assert "sub:conn_view:1" in user_callbacks

    node_met_kb = kb.node_metrics_keyboard("test-uuid")
    assert node_met_kb.inline_keyboard[0][0].callback_data == "nodes:metrics:test-uuid"

    geo_kb = kb.node_geocheck_keyboard("test-uuid")
    assert geo_kb.inline_keyboard[0][0].callback_data == "nodes:geocheck:test-uuid"

    digest_kb = kb.admin_stats_digest_keyboard()
    digest_cbs = [btn.callback_data for row in digest_kb.inline_keyboard for btn in row]
    assert "admin_stats:digest:24h" in digest_cbs
    assert "admin_stats:digest:7d" in digest_cbs
    assert "admin_stats:digest:30d" in digest_cbs
