import asyncio
from base64 import urlsafe_b64encode
from hashlib import sha256
import json
from pathlib import Path
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from .config import Settings


class LovableOAuthError(Exception):
    pass


class LovableOAuth:
    """OAuth 2.1 Authorization Code + PKCE support for Lovable MCP."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.oauth_store_file
        self._lock = asyncio.Lock()

    async def authorization_url(self) -> str:
        if not self.settings.lovable_oauth_client_id and not _is_loopback_url(
            self.settings.lovable_oauth_redirect_uri
        ):
            raise LovableOAuthError(
                "This is a hosted OAuth callback. Set LOVABLE_OAUTH_CLIENT_ID and "
                "LOVABLE_OAUTH_CLIENT_SECRET from a Lovable OAuth client that has this exact "
                "callback URL registered. Automatic registration is only suitable for localhost clients."
            )
        metadata = await self._discover()
        client_id, client_secret = await self._client_credentials(metadata)
        verifier = _url_safe_token(64)
        state = _url_safe_token(32)
        challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        async with self._lock:
            data = self._read()
            data.update(
                {
                    "oauth_state": state,
                    "code_verifier": verifier,
                    "state_expires_at": time.time() + 600,
                }
            )
            self._write(data)

        parameters = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self.settings.lovable_oauth_redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": self.settings.lovable_mcp_url,
        }
        if self.settings.lovable_oauth_scopes:
            parameters["scope"] = self.settings.lovable_oauth_scopes
        return f"{metadata['authorization_endpoint']}?{urlencode(parameters)}"

    async def exchange_callback(self, code: str, state: str) -> None:
        async with self._lock:
            data = self._read()
            expected_state = data.get("oauth_state")
            expires_at = data.get("state_expires_at", 0)
            verifier = data.get("code_verifier")
            if (
                not isinstance(expected_state, str)
                or not isinstance(verifier, str)
                or time.time() > expires_at
                or not secrets.compare_digest(state, expected_state)
            ):
                raise LovableOAuthError("The OAuth state is invalid or has expired. Start the login again.")
        token = await self._request_token(
            grant_type="authorization_code",
            code=code,
            code_verifier=verifier,
        )
        async with self._lock:
            data = self._read()
            self._store_token(data, token)

    async def access_token(self) -> str:
        async with self._lock:
            data = self._read()
            token = data.get("access_token")
            expires_at = data.get("expires_at", 0)
            if isinstance(token, str) and token and time.time() < expires_at - 60:
                return token

            refresh_token = data.get("refresh_token")
            if not isinstance(refresh_token, str) or not refresh_token:
                raise LovableOAuthError(
                    "Lovable is not connected. Open GET /auth/lovable/login and complete authorization."
                )
        refreshed = await self._request_token(
            grant_type="refresh_token",
            refresh_token=refresh_token,
        )
        async with self._lock:
            data = self._read()
            self._store_token(data, refreshed)
            new_token = refreshed.get("access_token")
            if not isinstance(new_token, str) or not new_token:
                raise LovableOAuthError("Lovable did not return an access token during refresh.")
            return new_token

    async def _discover(self) -> dict[str, Any]:
        resource_url = self.settings.lovable_mcp_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resource_response = await client.get(
                    f"{resource_url}/.well-known/oauth-protected-resource"
                )
                resource_response.raise_for_status()
                servers = resource_response.json().get("authorization_servers", [])
                if not servers or not isinstance(servers[0], str):
                    raise LovableOAuthError("Lovable did not publish an authorization server.")
                issuer = servers[0]
                metadata_response = await client.get(_authorization_metadata_url(issuer))
                metadata_response.raise_for_status()
                metadata = metadata_response.json()
        except httpx.HTTPError as exc:
            raise LovableOAuthError(f"Could not discover Lovable OAuth settings: {exc}") from exc

        if not isinstance(metadata.get("authorization_endpoint"), str) or not isinstance(metadata.get("token_endpoint"), str):
            raise LovableOAuthError("Lovable OAuth metadata is missing required endpoints.")
        return metadata

    async def _request_token(self, grant_type: str, **parameters: str) -> dict[str, Any]:
        metadata = await self._discover()
        client_id, client_secret = await self._client_credentials(metadata)
        payload = {
            "grant_type": grant_type,
            "client_id": client_id,
            "redirect_uri": self.settings.lovable_oauth_redirect_uri,
            **parameters,
        }
        if client_secret:
            payload["client_secret"] = client_secret
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(metadata["token_endpoint"], data=payload)
                response.raise_for_status()
                token = response.json()
        except httpx.HTTPError as exc:
            raise LovableOAuthError(f"Could not obtain a Lovable access token: {exc}") from exc
        if not isinstance(token, dict) or not isinstance(token.get("access_token"), str):
            raise LovableOAuthError("Lovable OAuth token response did not contain an access token.")
        return token

    async def _client_credentials(self, metadata: dict[str, Any]) -> tuple[str, str | None]:
        """Use explicit custom credentials only when configured; otherwise use DCR."""
        if self.settings.lovable_oauth_client_id:
            return self.settings.lovable_oauth_client_id, self.settings.lovable_oauth_client_secret

        async with self._lock:
            data = self._read()
            client_id = data.get("dynamic_client_id")
            if isinstance(client_id, str) and client_id:
                secret = data.get("dynamic_client_secret")
                return client_id, secret if isinstance(secret, str) else None

            registration_endpoint = metadata.get("registration_endpoint")
            if not isinstance(registration_endpoint, str):
                raise LovableOAuthError(
                    "Lovable did not advertise dynamic client registration. Configure a Lovable custom OAuth client instead."
                )
            registration = {
                "client_name": "Lovable Landing Pages API (local)",
                "redirect_uris": [self.settings.lovable_oauth_redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(registration_endpoint, json=registration)
                    response.raise_for_status()
                    registered = response.json()
            except httpx.HTTPError as exc:
                raise LovableOAuthError(f"Could not register this local OAuth client: {exc}") from exc

            client_id = registered.get("client_id") if isinstance(registered, dict) else None
            if not isinstance(client_id, str) or not client_id:
                raise LovableOAuthError("Lovable did not return a client ID for this local OAuth client.")
            client_secret = registered.get("client_secret")
            data["dynamic_client_id"] = client_id
            if isinstance(client_secret, str) and client_secret:
                data["dynamic_client_secret"] = client_secret
            self._write(data)
            return client_id, client_secret if isinstance(client_secret, str) else None

    def _store_token(self, data: dict[str, Any], token: dict[str, Any]) -> None:
        expires_in = token.get("expires_in", 3600)
        if not isinstance(expires_in, (int, float)):
            expires_in = 3600
        data.update(
            {
                "access_token": token["access_token"],
                "refresh_token": token.get("refresh_token", data.get("refresh_token")),
                "expires_at": time.time() + expires_in,
            }
        )
        data.pop("oauth_state", None)
        data.pop("code_verifier", None)
        data.pop("state_expires_at", None)
        self._write(data)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        if not isinstance(loaded, dict):
            raise LovableOAuthError("The local OAuth store is invalid. Delete it and sign in again.")
        return loaded

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        temporary_path.replace(self.path)


def _url_safe_token(bytes_length: int) -> str:
    return urlsafe_b64encode(secrets.token_bytes(bytes_length)).rstrip(b"=").decode()


def _authorization_metadata_url(issuer: str) -> str:
    parsed = urlsplit(issuer)
    if not parsed.scheme or not parsed.netloc:
        raise LovableOAuthError("Lovable returned an invalid OAuth issuer URL.")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/.well-known/oauth-authorization-server{parsed.path}", "", "")
    )


def _is_loopback_url(url: str) -> bool:
    hostname = urlsplit(url).hostname
    return hostname in {"localhost", "127.0.0.1", "::1"}
