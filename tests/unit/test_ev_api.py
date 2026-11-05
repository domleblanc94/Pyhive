"""EV transport headers, privacy, failures and the shared HTTP session."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from apyhiveapi.api.hive_async_api import HiveApiAsync
from apyhiveapi.helper.hive_exceptions import (
    HiveApiError,
    HiveAuthError,
    HiveConnectionError,
)


def _api(status=200, payload=None, token="sensitive-token"):
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value={} if payload is None else payload)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    websession = MagicMock()
    websession.request.return_value = response
    session = SimpleNamespace(tokens=SimpleNamespace(token_data={"token": token}))
    return HiveApiAsync(session, websession), websession, response


async def test_ev_calls_use_shared_session_exact_paths_and_headers(caplog):
    caplog.set_level("DEBUG", logger="apyhiveapi.api.hive_async_api")
    api, websession, _ = _api(payload={"homes": []})
    assert await api.get_ev_homes("account/one") == {"homes": []}
    args, kwargs = websession.request.call_args
    assert args == ("GET", "https://hive.bgchprod.info/home/owner/account%2Fone")
    assert kwargs["headers"]["Authorization"] == "Bearer sensitive-token"
    assert "x-api-key" in kwargs["headers"]
    assert kwargs["ssl"] is True
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"].total == 5
    assert await api.get_ev_nodes("home/one") == {"homes": []}
    args, kwargs = websession.request.call_args
    assert args == ("GET", "https://hive.bgchprod.info/omnia/nodes?homeId=home%2Fone")
    assert kwargs["headers"]["Accept"] == "application/vnd.alertme.zoo-6.5.0+json"
    assert kwargs["headers"]["X-AlertMe-Client"] == "Honeycomb React Native App"
    assert caplog.text == ""


@pytest.mark.parametrize("token", [None, "", 123])
async def test_ev_token_missing_is_auth_error_without_http(token):
    api, websession, _ = _api(token=token)
    with pytest.raises(HiveAuthError, match="authorization is missing"):
        await api.get_ev_homes("account-one")
    websession.request.assert_not_called()


async def test_ev_api_without_session_fails_explicitly():
    api = HiveApiAsync()
    with pytest.raises(HiveAuthError):
        await api.get_ev_homes("account-one")


@pytest.mark.parametrize("status", [401, 403])
async def test_ev_auth_http_status_has_no_private_url_or_body(status, caplog):
    caplog.set_level("DEBUG", logger="apyhiveapi.api.hive_async_api")
    api, _, response = _api(status, {"secret": "response-sensitive"})
    with pytest.raises(HiveAuthError) as caught:
        await api.get_ev_homes("account-sensitive")
    assert "account-sensitive" not in str(caught.value)
    assert "response-sensitive" not in str(caught.value)
    response.json.assert_not_awaited()
    assert caplog.text == ""


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_ev_temporary_http_errors_are_connection_errors(status):
    api, _, _ = _api(status)
    with pytest.raises(HiveConnectionError):
        await api.get_ev_homes("account-one")


@pytest.mark.parametrize("status", [302, 404])
async def test_ev_redirects_and_unexpected_statuses_fail_without_retry(status):
    api, websession, _ = _api(status)
    with pytest.raises(HiveApiError):
        await api.get_ev_homes("account-one")
    websession.request.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [aiohttp.ClientError("sensitive"), OSError("sensitive"), asyncio.TimeoutError()],
)
async def test_ev_transport_errors_are_sanitized(error, caplog):
    caplog.set_level("DEBUG", logger="apyhiveapi.api.hive_async_api")
    api, websession, _ = _api()
    websession.request.side_effect = error
    with pytest.raises(HiveConnectionError) as caught:
        await api.get_ev_homes("account-sensitive")
    assert "sensitive" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caplog.text == ""


async def test_ev_non_object_json_fails():
    api, _, _ = _api(payload=[])
    with pytest.raises(HiveApiError, match="invalid representation"):
        await api.get_ev_homes("account-one")


async def test_ev_invalid_json_fails_without_raw_error():
    api, _, response = _api()
    response.json.side_effect = ValueError("sensitive-response")
    with pytest.raises(HiveApiError) as caught:
        await api.get_ev_homes("account-one")
    assert "sensitive-response" not in str(caught.value)
