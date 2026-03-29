"""Lightweight REST client for direct HTTP calls to the EV charger."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Supported HTTP methods
SUPPORTED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


class ChargerRestClient:
    """
    Async REST client targeting the charger's built-in HTTP API.

    Authentication supports:
      - None
      - HTTP Basic  (username + password)
      - Bearer token
    """

    def __init__(
        self,
        base_url: str,
        auth_type: str = "none",
        username: str = "",
        password: str = "",
        bearer_token: str = "",
        timeout: int = 10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_type = auth_type.lower()
        self.username = username
        self.password = password
        self.bearer_token = bearer_token
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    def _auth(self) -> aiohttp.BasicAuth | None:
        if self.auth_type == "basic":
            return aiohttp.BasicAuth(self.username, self.password)
        return None

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    async def call(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> dict[str, Any]:
        """
        Perform an HTTP request.

        Returns a dict with:
          status_code  – int
          ok           – bool
          body         – parsed JSON, raw text, or error message
          headers      – response headers as dict
          url          – final URL called
        """
        method = method.upper()
        if method not in SUPPORTED_METHODS:
            return {
                "status_code": None,
                "ok": False,
                "body": f"Unsupported method: {method}",
                "headers": {},
                "url": "",
            }

        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        try:
            async with aiohttp.ClientSession(
                auth=self._auth(),
                headers=self._headers(),
                timeout=self.timeout,
            ) as session:
                kwargs: dict[str, Any] = {"params": params}
                if body is not None:
                    if isinstance(body, (dict, list)):
                        kwargs["json"] = body
                    else:
                        kwargs["data"] = str(body)

                async with session.request(method, url, **kwargs) as resp:
                    status = resp.status
                    resp_headers = dict(resp.headers)
                    content_type = resp.content_type or ""

                    if "json" in content_type:
                        try:
                            parsed = await resp.json(content_type=None)
                        except Exception:
                            parsed = await resp.text()
                    else:
                        parsed = await resp.text()

                    _LOGGER.debug(
                        "REST %s %s → %d", method, url, status
                    )
                    return {
                        "status_code": status,
                        "ok": 200 <= status < 300,
                        "body": parsed,
                        "headers": resp_headers,
                        "url": url,
                    }

        except aiohttp.ClientConnectorError as err:
            return {"status_code": None, "ok": False,
                    "body": f"Connection error: {err}", "headers": {}, "url": url}
        except asyncio.TimeoutError:
            return {"status_code": None, "ok": False,
                    "body": f"Request timed out after {self.timeout.total}s",
                    "headers": {}, "url": url}
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("REST call failed")
            return {"status_code": None, "ok": False,
                    "body": f"Unexpected error: {err}", "headers": {}, "url": url}
