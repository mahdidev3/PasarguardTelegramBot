"""Async Pasarguard API client for Phase 4.

This module is intentionally isolated from Telegram handlers and database code.
It wraps the OpenAPI endpoints used by the bot and keeps auth/retry/error
handling in one place.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings


class PasarguardConfigurationError(RuntimeError):
    pass


class PasarguardAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, response: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response


def _permission_hint(status_code: int, method: str, path: str, body: Any) -> str:
    """Return a Persian operator-friendly hint for common Pasarguard permission failures."""
    if status_code != 403:
        return ""
    if path.startswith("/api/user_template") or path.startswith("/api/user_templates"):
        return (
            "؛ احراز هویت موفق است اما این ادمین اجازه مدیریت User Template را ندارد. "
            "یک ادمین sudo/super استفاده کن یا permission ساخت/ویرایش template را برای این اکانت فعال کن."
        )
    if path.startswith("/api/user"):
        return (
            "؛ احراز هویت موفق است اما این ادمین اجازه مدیریت User/User Subscription را ندارد. "
            "permission ساخت/ویرایش/ریست کاربر را در Pasarguard بررسی کن."
        )
    return "؛ احراز هویت موفق است اما سطح دسترسی این ادمین برای این عملیات کافی نیست."


@dataclass(frozen=True)
class PasarguardConnectionInfo:
    enabled: bool
    base_url: str
    dry_run: bool
    username: str
    managed_prefix: str
    group_ids: list[int]


def connection_info() -> PasarguardConnectionInfo:
    return PasarguardConnectionInfo(
        enabled=settings.pasarguard_enabled,
        base_url=settings.pasarguard_base_url,
        dry_run=settings.pasarguard_dry_run,
        username=settings.pasarguard_admin_username,
        managed_prefix=settings.pasarguard_managed_prefix,
        group_ids=list(settings.pasarguard_template_group_ids),
    )


class PasarguardClient:
    def __init__(self) -> None:
        if not settings.pasarguard_base_url:
            raise PasarguardConfigurationError("PASARGUARD_BASE_URL تنظیم نشده است.")
        self.base_url = settings.pasarguard_base_url.rstrip("/")
        self._access_token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.pasarguard_timeout_seconds,
            verify=settings.pasarguard_verify_ssl,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PasarguardClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        await self.aclose()

    def _headers(self) -> dict[str, str]:
        if not self._access_token:
            return {}
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _request(self, method: str, path: str, *, retries: int = 1, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                headers = dict(kwargs.pop("headers", {}) or {})
                headers.update(self._headers())
                response = await self._client.request(method, path, headers=headers, **kwargs)
                if response.status_code == 401 and self._access_token and path != "/api/admin/token":
                    self._access_token = None
                    await self.login()
                    headers = dict(kwargs.pop("headers", {}) or {})
                    headers.update(self._headers())
                    response = await self._client.request(method, path, headers=headers, **kwargs)
                if response.status_code >= 400:
                    try:
                        body = response.json()
                    except Exception:
                        body = response.text
                    hint = _permission_hint(response.status_code, method, path, body)
                    raise PasarguardAPIError(
                        f"Pasarguard API error {response.status_code} on {method} {path}{hint}",
                        status_code=response.status_code,
                        response=body,
                    )
                if response.status_code == 204 or not response.content:
                    return None
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type:
                    return response.json()
                try:
                    return response.json()
                except Exception:
                    return response.text
            except (httpx.TimeoutException, httpx.NetworkError, PasarguardAPIError) as exc:
                last_exc = exc
                if attempt >= retries:
                    break
                await asyncio.sleep(0.6 * (attempt + 1))
        if isinstance(last_exc, PasarguardAPIError):
            raise last_exc
        raise PasarguardAPIError(str(last_exc or "unknown Pasarguard error"))

    async def health(self) -> Any:
        return await self._request("GET", "/health", retries=1)

    async def login(self) -> str:
        if not settings.pasarguard_admin_username or not settings.pasarguard_admin_password:
            raise PasarguardConfigurationError("PASARGUARD_ADMIN_USERNAME/PASARGUARD_ADMIN_PASSWORD تنظیم نشده است.")
        data = {
            "username": settings.pasarguard_admin_username,
            "password": settings.pasarguard_admin_password,
            "grant_type": "password",
            "scope": "",
        }
        response = await self._request("POST", "/api/admin/token", data=data, retries=1)
        token = str(response.get("access_token") or "") if isinstance(response, dict) else ""
        if not token:
            raise PasarguardAPIError("توکن از Pasarguard دریافت نشد.", response=response)
        self._access_token = token
        return token

    async def ensure_login(self) -> None:
        if not self._access_token:
            await self.login()

    async def current_admin(self) -> Any:
        await self.ensure_login()
        return await self._request("GET", "/api/admin", retries=1)

    @staticmethod
    def _extract_list_response(data: Any, *keys: str) -> list[dict[str, Any]]:
        """Extract Pasarguard list responses with varied wrappers.

        The OpenAPI schemas use response objects for some list endpoints, while
        older deployments may return a bare list. Keep this defensive so sync and
        backup do not silently miss records after panel upgrades.
        """
        if isinstance(data, dict):
            for key in (*keys, "items", "data", "results", "users", "user_templates", "templates"):
                if isinstance(data.get(key), list):
                    return [dict(item) for item in data[key] if isinstance(item, dict)]
        if isinstance(data, list):
            return [dict(item) for item in data if isinstance(item, dict)]
        return []

    async def list_user_templates(self, *, limit: int = 1000, offset: int = 0) -> list[dict[str, Any]]:
        await self.ensure_login()
        data = await self._request("GET", "/api/user_templates", params={"limit": limit, "offset": offset}, retries=1)
        return self._extract_list_response(data, "user_templates", "templates")

    async def list_users(self, *, limit: int = 1000, offset: int = 0, search: str | None = None) -> list[dict[str, Any]]:
        await self.ensure_login()
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if search:
            params["search"] = search
        data = await self._request("GET", "/api/users", params=params, retries=1)
        return self._extract_list_response(data, "users")

    async def get_user_template(self, template_id: int) -> dict[str, Any]:
        await self.ensure_login()
        return await self._request("GET", f"/api/user_template/{template_id}", retries=1)

    async def create_user_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_login()
        return await self._request("POST", "/api/user_template", json=payload, retries=1)

    async def update_user_template(self, template_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_login()
        return await self._request("PUT", f"/api/user_template/{template_id}", json=payload, retries=1)

    async def bulk_disable_user_templates(self, template_ids: list[int]) -> Any:
        await self.ensure_login()
        return await self._request("POST", "/api/user_templates/bulk/disable", json={"ids": template_ids}, retries=1)

    async def bulk_enable_user_templates(self, template_ids: list[int]) -> Any:
        await self.ensure_login()
        return await self._request("POST", "/api/user_templates/bulk/enable", json={"ids": template_ids}, retries=1)

    async def create_user_from_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_login()
        return await self._request("POST", "/api/user/from_template", json=payload, retries=1)

    async def modify_user_with_template(self, username: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_login()
        return await self._request("PUT", f"/api/user/from_template/by-username/{username}", json=payload, retries=1)

    async def get_user_by_username(self, username: str) -> dict[str, Any]:
        await self.ensure_login()
        return await self._request("GET", f"/api/user/by-username/{username}", retries=1)

    async def update_user_by_username(self, username: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self.ensure_login()
        return await self._request("PUT", f"/api/user/by-username/{username}", json=payload, retries=1)

    async def reset_user_usage(self, username: str) -> dict[str, Any]:
        await self.ensure_login()
        return await self._request("POST", f"/api/user/by-username/{username}/reset", retries=1)

    async def revoke_user_subscription(self, username: str) -> dict[str, Any]:
        await self.ensure_login()
        return await self._request("POST", f"/api/user/by-username/{username}/revoke_sub", retries=1)









