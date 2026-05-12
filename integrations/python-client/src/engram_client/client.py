"""Async Python client for the engram semantic memory API.

Designed for web apps (Django, FastAPI) that need project-scoped memory
with configurable namespace isolation.

Usage::

    engram = EngramClient(
        url="http://localhost:8920",
        token="engram_...",
        namespace="coursebuilder-ixanadu",
        project="ProjAlpha",
    )

    results = await engram.search("quiz generation strategies")
    await engram.store("decision/quiz-format", "Multiple choice with explanations")
    memory = await engram.get("decision/quiz-format")
    await engram.forget("decision/quiz-format")
    await engram.close()
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx


@dataclass
class EngramClient:
    """Client for engram semantic memory API.

    Args:
        url: Engram server URL.
        token: Bearer token for authentication (tied to a principal).
        namespace: Default namespace for writes. This is the read/write boundary.
        project: Default project name (sent in the ``project`` column, separate
            from ``user_id`` after Phase 4 of the identity model).
        user_id: Explicit user_id (the person who owns writes). When omitted,
            the SDK calls ``/whoami`` on first use and caches the principal
            name from the token. Pass this only if you need to override.
        read_namespaces: Additional namespaces to search. Useful during dev when
            terminal (claude-code) and web app (coursebuilder) share memories.
            The primary namespace is always included automatically.
        scope: Default scope. Almost always "project" for web apps.
        timeout: Request timeout in seconds.
    """

    url: str
    token: str
    namespace: str
    project: str
    user_id: str | None = None
    read_namespaces: list[str] = field(default_factory=list)
    scope: str = "project"
    timeout: float = 30.0
    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _resolved_user_id: str | None = field(default=None, repr=False)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._client = httpx.AsyncClient(
                base_url=self.url.rstrip("/"),
                headers=headers,
                timeout=self.timeout,
            )
        return self._client

    def _search_namespaces(self) -> list[str]:
        ns = [self.namespace]
        for extra in self.read_namespaces:
            if extra not in ns:
                ns.append(extra)
        return ns

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        client = self._get_client()
        for attempt in range(2):
            try:
                resp = await client.request(method, path, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise

    async def _resolve_user_id(self) -> str:
        """Return the user_id to send on writes. Order: explicit > /whoami > 'unknown'.

        After Phase 4, ``user_id`` is the person and ``project`` is separate.
        Web apps authenticate with a token but don't usually know the principal
        name behind it, so we ask the server once and cache.
        """
        if self.user_id:
            return self.user_id
        if self._resolved_user_id:
            return self._resolved_user_id
        try:
            data = await self._request("GET", "/whoami")
            name = data.get("name") if isinstance(data, dict) else None
            self._resolved_user_id = name or "unknown"
        except Exception:
            self._resolved_user_id = "unknown"
        return self._resolved_user_id

    async def search(
        self,
        query: str,
        *,
        project: str | None = None,
        scope: str | None = None,
        limit: int = 5,
        namespaces: list[str] | None = None,
    ) -> list[dict]:
        """Search memories semantically. Returns matching items.

        Searches across the primary namespace plus any configured
        read_namespaces by default. Override with ``namespaces`` param.
        """
        data = await self._request(
            "POST",
            "/memory/search",
            json={
                "namespaces": namespaces or self._search_namespaces(),
                "query": query,
                "scope": scope or self.scope,
                "user_id": await self._resolve_user_id(),
                "project": project or self.project,
                "limit": limit,
            },
        )
        return data.get("results", [])

    async def store(
        self,
        key: str,
        value: str,
        *,
        tags: str = "",
        project: str | None = None,
        scope: str | None = None,
        namespace: str | None = None,
        expiration_days: int = 180,
    ) -> str:
        """Store or update a memory. Returns the key."""
        data = await self._request(
            "POST",
            "/memory/set",
            json={
                "namespace": namespace or self.namespace,
                "key": key,
                "value": value,
                "scope": scope or self.scope,
                "user_id": await self._resolve_user_id(),
                "project": project or self.project,
                "tags": tags,
                "expiration_days": expiration_days,
            },
        )
        return data.get("key", key)

    async def get(
        self,
        key: str,
        *,
        project: str | None = None,
        scope: str | None = None,
        namespace: str | None = None,
    ) -> dict | None:
        """Get a memory by exact key. Returns the item or None."""
        data = await self._request(
            "POST",
            "/memory/get",
            json={
                "namespace": namespace or self.namespace,
                "key": key,
                "scope": scope or self.scope,
                "user_id": await self._resolve_user_id(),
                "project": project or self.project,
            },
        )
        return data.get("memory")

    async def forget(
        self,
        key: str,
        *,
        project: str | None = None,
        scope: str | None = None,
        namespace: str | None = None,
    ) -> bool:
        """Delete a memory by key. Returns True if it existed."""
        data = await self._request(
            "POST",
            "/memory/forget",
            json={
                "namespace": namespace or self.namespace,
                "key": key,
                "scope": scope or self.scope,
                "user_id": await self._resolve_user_id(),
                "project": project or self.project,
            },
        )
        return data.get("status") == "ok"

    async def whoami(self) -> dict:
        """Return the authenticated principal record. Triggers user_id cache."""
        return await self._request("GET", "/whoami")

    async def health(self) -> dict:
        """Check engram server health."""
        return await self._request("GET", "/health")

    async def close(self):
        """Close the underlying HTTP connection pool."""
        if self._client:
            await self._client.aclose()
            self._client = None
