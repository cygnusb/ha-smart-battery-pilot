"""Read-only access to evcc's REST API.

Only GET requests, through Home Assistant's shared HTTP session. evcc is an
optional input: every failure ends up as EvccError, and the caller plans
without EV sessions instead of failing.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant

REQUEST_TIMEOUT_SECONDS = 5


class EvccError(Exception):
    """evcc could not be read."""


def normalize_url(url: str) -> str:
    """`host:port` or a full URL, without a trailing slash."""
    url = url.strip()
    if "://" not in url:
        url = f"http://{url}"
    return url.rstrip("/")


async def _get_json(hass: HomeAssistant, url: str) -> Any:
    # Imported here: the stub test suite has no aiohttp, and callers there
    # replace the fetch functions as a whole.
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            response = await session.get(url)
            response.raise_for_status()
            return await response.json(content_type=None)
    except TimeoutError as err:
        raise EvccError(f"timeout reading {url}") from err
    except Exception as err:  # aiohttp errors, bad JSON
        raise EvccError(f"{type(err).__name__} reading {url}") from err


def _unwrap(payload: Any) -> Any:
    # Older evcc versions wrap every response in {"result": ...}.
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


async def async_fetch_state(hass: HomeAssistant, base_url: str) -> dict[str, Any]:
    """`/api/state`, checked to look like evcc's."""
    state = _unwrap(await _get_json(hass, f"{base_url}/api/state"))
    if not isinstance(state, dict) or not isinstance(state.get("loadpoints"), list):
        raise EvccError("response is not an evcc state")
    return state


async def async_fetch_plan_power(
    hass: HomeAssistant, base_url: str, state: dict[str, Any]
) -> dict[int, float]:
    """Charge power each active plan was sized with, by loadpoint id.

    Only loadpoints with a plan are asked; a failure leaves that loadpoint
    out, and the session falls back to current x phases.
    """
    power: dict[int, float] = {}
    for index, lp in enumerate(state.get("loadpoints") or []):
        if not isinstance(lp, dict) or not lp.get("plan"):
            continue
        try:
            plan = _unwrap(await _get_json(hass, f"{base_url}/api/loadpoints/{index + 1}/plan"))
        except EvccError:
            continue
        value = plan.get("power") if isinstance(plan, dict) else None
        if isinstance(value, (int, float)) and value > 0:
            power[index + 1] = float(value)
    return power
