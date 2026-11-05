"""Optional EV monitoring must not affect legacy Hive device discovery."""

# pylint: disable=protected-access
import base64
import copy
import json
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apyhiveapi.devices.ev import EVCharger, HiveEV, _account_id, _parse_node
from apyhiveapi.helper.hive_exceptions import (
    HiveApiError,
    HiveAuthError,
    HiveConnectionError,
    HiveReauthRequired,
)

EVSE = "http://alertme.com/schema/json/node.class.evse.json#"


def _token(claims=None):
    body = json.dumps(claims or {"cognito:username": "account-one"}).encode()
    return (
        "header." + base64.urlsafe_b64encode(body).decode().rstrip("=") + ".signature"
    )


def _node(status="SCHEDULED", active=True):
    return {
        "id": "evse-one",
        "nodeType": EVSE,
        "name": "Garage",
        "features": {
            "ev_supply_equipment_v1": {
                "displayState": {"reportedValue": status, "targetValue": "CHARGING"},
                "transactionActive": {"reportedValue": active, "targetValue": False},
                "transactionActivePowerImportW": {"targetValue": 7400},
            }
        },
    }


def _session():
    return SimpleNamespace(
        config=SimpleNamespace(file=False),
        tokens=SimpleNamespace(token_data={"token": _token()}),
        hive_refresh_tokens=AsyncMock(),
        api=SimpleNamespace(
            get_ev_homes=AsyncMock(return_value={"homes": [{"id": "home-one"}]}),
            get_ev_nodes=AsyncMock(return_value={"nodes": [_node()]}),
        ),
        data={"products": {"heating-one": {"state": {"target": 20}}}},
    )


async def test_ev_snapshot_uses_reported_fields_only():
    session = _session()
    devices = await HiveEV(session).get_devices()
    device = devices["evse-one"]
    assert isinstance(device, EVCharger)
    assert device.home_id == "home-one"
    assert device.name == "Garage"
    assert device.status == "scheduled"
    # Active transaction is not the same as current physical charging.
    assert device.transaction_active is True
    assert device.power_w is None
    assert device.session_energy_wh is None
    assert device.session_duration_s is None
    session.hive_refresh_tokens.assert_awaited_once_with()
    session.api.get_ev_homes.assert_awaited_once_with("account-one")
    with pytest.raises(FrozenInstanceError):
        device.status = "charging"
    assert "evse-one" not in repr(device)
    assert "Garage" not in repr(device)


def test_known_numeric_units_and_missing_readings():
    node = _node("CHARGING", False)
    feature = node["features"]["ev_supply_equipment_v1"]
    feature["transactionActivePowerImportW"]["reportedValue"] = 7400
    feature["transactionEnergyTotalWh"] = {"reportedValue": 1234.5}
    feature["transactionChargeTimeSeconds"] = {"reportedValue": 600}
    device = _parse_node(node, "home-one")
    assert device.power_w == 7400
    assert device.session_energy_wh == 1234.5
    assert device.session_duration_s == 600
    assert device.transaction_active is False


@pytest.mark.parametrize(
    "value", [None, False, "0", -1, float("nan"), float("inf"), 10**1000]
)
def test_invalid_numeric_fields_remain_unknown(value):
    node = _node()
    node["features"]["ev_supply_equipment_v1"]["transactionActivePowerImportW"] = {
        "reportedValue": value
    }
    assert _parse_node(node, "home-one").power_w is None


def test_unknown_state_or_malformed_optional_values_are_not_inferred():
    node = _node({"unexpected": "value"}, "true")
    node["name"] = " "
    node["features"]["ev_supply_equipment_v1"]["transactionEnergyTotalWh"] = []
    device = _parse_node(node, "home-one")
    assert device.status is None
    assert device.transaction_active is None
    assert device.name == "Hive EV charger"
    assert device.session_energy_wh is None
    node["features"]["ev_supply_equipment_v1"]["displayState"] = {
        "reportedValue": "FUTURE_VENDOR_STATUS"
    }
    assert _parse_node(node, "home-one").status is None
    node.pop("name")
    assert _parse_node(node, "home-one").name == "Hive EV charger"


@pytest.mark.parametrize(
    "token",
    [
        None,
        "invalid",
        "x.e30.y",
        "x.W10.y",
        "x./.y",
        _token({"sub": "not-username"}),
        _token({"cognito:username": ""}),
        _token({"cognito:username": "x" * 257}),
    ],
)
def test_missing_or_unusable_cognito_account_requires_reauth(token):
    with pytest.raises(HiveReauthRequired, match="valid account sign-in"):
        _account_id(token)


@pytest.mark.parametrize(
    "homes",
    [
        None,
        {},
        [None],
        [{"id": ""}],
        [{"id": "x" * 257}],
        [{"id": f"home-{number}"} for number in range(11)],
    ],
)
async def test_invalid_homes_fail_explicitly(homes):
    session = _session()
    session.api.get_ev_homes.return_value = {"homes": homes}
    with pytest.raises(HiveApiError, match="home representation"):
        await HiveEV(session).get_devices()


@pytest.mark.parametrize("nodes", [None, {}, [None]])
async def test_invalid_nodes_fail_explicitly(nodes):
    session = _session()
    session.api.get_ev_nodes.return_value = {"nodes": nodes}
    with pytest.raises(HiveApiError, match="node representation"):
        await HiveEV(session).get_devices()


@pytest.mark.parametrize(
    "changes",
    [
        {"id": None},
        {"id": ""},
        {"id": "x" * 257},
        {"features": None},
        {"features": {"ev_supply_equipment_v1": []}},
    ],
)
def test_incomplete_evse_fails_instead_of_retaining_stale_data(changes):
    node = _node()
    node.update(changes)
    with pytest.raises(HiveApiError, match="charger representation"):
        _parse_node(node, "home-one")


async def test_no_ev_and_multiple_homes_without_a_legacy_hub():
    session = _session()
    session.api.get_ev_homes.return_value = {"homes": [{"id": "h1"}, {"id": "h2"}]}
    other = _node("AVAILABLE", False)
    other["id"] = "evse-two"
    session.api.get_ev_nodes.side_effect = [
        {"nodes": [{"nodeType": "station"}, _node()]},
        {"nodes": [other]},
    ]
    assert set(await HiveEV(session).get_devices()) == {"evse-one", "evse-two"}
    session.api.get_ev_nodes.side_effect = None
    session.api.get_ev_nodes.return_value = {"nodes": []}
    assert await HiveEV(session).get_devices() == {}
    session.api.get_ev_homes.return_value = {"homes": []}
    assert await HiveEV(session).get_devices() == {}


async def test_duplicate_evse_ids_fail_instead_of_overwriting():
    session = _session()
    session.api.get_ev_nodes.return_value = {"nodes": [_node(), _node()]}
    with pytest.raises(HiveApiError, match="duplicate"):
        await HiveEV(session).get_devices()


async def test_ev_auth_error_uses_shared_refresh_once():
    session = _session()
    session.api.get_ev_homes.side_effect = [
        HiveAuthError("rejected"),
        {"homes": [{"id": "home-one"}]},
    ]
    assert "evse-one" in await HiveEV(session).get_devices()
    assert session.hive_refresh_tokens.await_count == 2
    session.hive_refresh_tokens.assert_awaited_with(force_refresh=True)


async def test_repeated_auth_rejection_requires_reauth_without_loop():
    session = _session()
    session.api.get_ev_homes.side_effect = HiveAuthError("account-sensitive")
    with pytest.raises(HiveReauthRequired) as caught:
        await HiveEV(session).get_devices()
    assert "account-sensitive" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert session.api.get_ev_homes.await_count == 2


async def test_ev_failure_does_not_replace_legacy_heating_data():
    session = _session()
    before = copy.deepcopy(session.data)
    session.api.get_ev_nodes.side_effect = HiveConnectionError("unavailable")
    with pytest.raises(HiveConnectionError):
        await HiveEV(session).get_devices()
    assert session.data == before


async def test_existing_file_session_never_queries_ev_service(file_session):
    before = copy.deepcopy(file_session.data)
    file_session.api.get_ev_homes = AsyncMock(side_effect=AssertionError("No EV call"))
    assert await file_session.ev.get_devices() == {}
    file_session.api.get_ev_homes.assert_not_awaited()
    assert file_session.data == before


@pytest.mark.parametrize("missing", ["get_ev_homes", "get_ev_nodes"])
async def test_unsupported_backend_fails_clearly_before_auth(missing):
    session = _session()
    delattr(session.api, missing)
    with pytest.raises(HiveApiError, match="asynchronous Hive API"):
        await HiveEV(session).get_devices()
    session.hive_refresh_tokens.assert_not_awaited()
