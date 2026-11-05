"""Optional read-only Hive EV discovery using the existing account session."""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeGuard

from ..helper.hive_exceptions import HiveApiError, HiveAuthError, HiveReauthRequired

if TYPE_CHECKING:
    from ..session import HiveSession

_EVSE_TYPE = "http://alertme.com/schema/json/node.class.evse.json#"
_EVSE_FEATURE = "ev_supply_equipment_v1"
_MAX_HOMES = 10
_MAX_IDENTIFIER_LENGTH = 256
_MAX_READING = 1e12
EV_STATUSES = (
    "offline",
    "available",
    "charging",
    "finishing",
    "faulted",
    "unknown",
    "unavailable",
    "ready",
    "complete",
    "completed",
    "paused",
    "scheduled",
    "waiting_to_start",
    "waiting_to_resume",
    "waiting_for_vehicle",
    "waiting_for_vehicle_not_charging",
    "unrecognized",
)


@dataclass(frozen=True)
class EVCharger:
    """A reported EVSE snapshot, independent of the legacy Hive hub.

    ``transaction_active`` denotes a charging session, not physical power draw.
    In particular, an active session can be Scheduled. ``available`` does not
    establish cable connection. Missing telemetry remains ``None``.
    """

    node_id: str = field(repr=False)
    home_id: str = field(repr=False)
    name: str = field(repr=False)
    status: str | None
    transaction_active: bool | None
    power_w: float | None
    session_energy_wh: float | None
    session_duration_s: float | None


def _identifier(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= _MAX_IDENTIFIER_LENGTH


def _account_id(token: Any) -> str:
    """Decode metadata from the session's authenticated Cognito ID token.

    This is not a signature verifier or a way to accept external JWTs. The
    existing session's authentication and token renewal remain authoritative.
    """
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        user_id = claims.get("cognito:username")
        if _identifier(user_id):
            return user_id
    except (AttributeError, IndexError, TypeError, ValueError):
        pass
    raise HiveReauthRequired("Hive EV requires a valid account sign-in")


def _reported(feature: dict, attribute: str) -> Any:
    wrapper = feature.get(attribute)
    return wrapper.get("reportedValue") if isinstance(wrapper, dict) else None


def _number(value: Any) -> float | None:
    if (
        type(value) in (int, float)
        and 0 <= value <= _MAX_READING
        and math.isfinite(value)
    ):
        return float(value)
    return None


def _parse_node(node: dict, home_id: str) -> EVCharger:
    node_id = node.get("id")
    features = node.get("features")
    feature = features.get(_EVSE_FEATURE) if isinstance(features, dict) else None
    if not _identifier(node_id) or not isinstance(feature, dict):
        raise HiveApiError("Hive EV charger representation changed")
    status = _reported(feature, "displayState")
    status = status.lower() if isinstance(status, str) else None
    if status not in EV_STATUSES:
        status = None
    active = _reported(feature, "transactionActive")
    name = node.get("name")
    return EVCharger(
        node_id=node_id,
        home_id=home_id,
        name=name.strip()[:100]
        if isinstance(name, str) and name.strip()
        else "Hive EV charger",
        status=status,
        transaction_active=active if isinstance(active, bool) else None,
        power_w=_number(_reported(feature, "transactionActivePowerImportW")),
        session_energy_wh=_number(_reported(feature, "transactionEnergyTotalWh")),
        session_duration_s=_number(_reported(feature, "transactionChargeTimeSeconds")),
    )


class HiveEV:
    """Separate EV reads cannot replace or invalidate legacy device data.

    Consumers explicitly call ``get_devices`` at their chosen polling interval.
    No EV service calls are added to legacy discovery or heating/light polling.
    """

    statuses = EV_STATUSES

    def __init__(self, session: HiveSession) -> None:
        self.session = session

    async def _read_devices(self) -> dict[str, EVCharger]:
        user_id = _account_id(self.session.tokens.token_data.get("token"))
        payload = await self.session.api.get_ev_homes(user_id)
        homes = payload.get("homes")
        if (
            not isinstance(homes, list)
            or len(homes) > _MAX_HOMES
            or not all(
                isinstance(home, dict) and _identifier(home.get("id")) for home in homes
            )
        ):
            raise HiveApiError("Hive EV home representation changed")
        devices = {}
        for home in homes:
            payload = await self.session.api.get_ev_nodes(home["id"])
            nodes = payload.get("nodes")
            if not isinstance(nodes, list) or not all(
                isinstance(node, dict) for node in nodes
            ):
                raise HiveApiError("Hive EV node representation changed")
            for node in nodes:
                if node.get("nodeType") == _EVSE_TYPE:
                    charger = _parse_node(node, home["id"])
                    if charger.node_id in devices:
                        raise HiveApiError(
                            "Hive EV returned duplicate charger identifiers"
                        )
                    devices[charger.node_id] = charger
        return devices

    async def get_devices(self) -> dict[str, EVCharger]:
        """Return reported chargers, sharing authentication and token refresh.

        Raise ``HiveConnectionError`` for transport failures, ``HiveApiError``
        for an incompatible response, and ``HiveReauthRequired`` when sign-in
        must be renewed. Do not treat an API error as an empty charger list.
        """
        if self.session.config.file:
            return {}
        if not callable(
            getattr(self.session.api, "get_ev_homes", None)
        ) or not callable(getattr(self.session.api, "get_ev_nodes", None)):
            raise HiveApiError("EV monitoring requires the asynchronous Hive API")
        await self.session.hive_refresh_tokens()
        try:
            return await self._read_devices()
        except HiveAuthError:
            await self.session.hive_refresh_tokens(force_refresh=True)
        try:
            return await self._read_devices()
        except HiveAuthError:
            raise HiveReauthRequired("Hive EV authorization was rejected") from None
