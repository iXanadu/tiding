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
import socket
from dataclasses import dataclass, field

import httpx


@dataclass
class EngramClient:
    """Client for engram semantic memory API.

    Args:
        url: Engram server URL.
        token: Bearer token for authentication (tied to a principal).
        namespace: Default namespace for writes. This is the write boundary.
        project: Default project name (sent in the ``project`` column, separate
            from ``user_id`` after Phase 4 of the identity model).
        user_id: Explicit user_id (the person who owns writes). When omitted,
            the SDK calls ``/whoami`` on first use and caches the principal
            name from the token. Pass this only if you need to override.
        read_namespaces: Search scope override. ``None`` (default) means the
            server resolves from the principal's ``read_namespaces`` — for an
            admin/wildcard token this spans everything. Pass a list to narrow:
            ``["claude-code"]`` searches only that namespace; the primary
            ``namespace`` is auto-included.
        reader_identity: Default inbox identity for this client. When set
            explicitly (e.g. ``"projalpha@laptop"`` for a CC project
            session), it's used as-is. When ``None`` (default), the SDK
            resolves it from ``/whoami`` and uses the principal name — the
            right default for apps that speak as their token's owner (a
            chat client authenticating with the human's token gets the
            human's name as its inbox address).
        scope: Default scope. ``"project"`` or ``"user"`` for web apps.
        enabled: Kill switch. When ``False``, :meth:`is_available` short-circuits
            to ``False`` so an app can run with memory turned off without
            changing call sites.
        timeout: Request timeout in seconds.
    """

    url: str
    token: str
    namespace: str
    project: str
    user_id: str | None = None
    read_namespaces: list[str] | None = None
    reader_identity: str | None = None
    scope: str = "project"
    enabled: bool = True
    timeout: float = 30.0
    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _resolved_user_id: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, prefix: str, **overrides) -> "EngramClient":
        """Build a client from app-prefixed environment variables.

        Reads ``<PREFIX>_ENGRAM_{URL,TOKEN,NAMESPACE,PROJECT,SCOPE,ENABLED}``.
        Defaults: URL ``http://localhost:8920``, SCOPE ``user`` (web-app
        default), ENABLED ``true``. ``prefix`` is case-insensitive; a trailing
        underscore is ignored. Keyword ``overrides`` win over the environment.

        Example::

            engram = EngramClient.from_env("BEASTCHAT")  # BEASTCHAT_ENGRAM_*
        """
        import os

        p = prefix.rstrip("_").upper()

        def g(suffix: str, default: str = "") -> str:
            return os.environ.get(f"{p}_ENGRAM_{suffix}", default)

        enabled = g("ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
        kwargs: dict = dict(
            url=g("URL", "http://localhost:8920"),
            token=g("TOKEN"),
            namespace=g("NAMESPACE"),
            project=g("PROJECT"),
            scope=g("SCOPE", "user"),
            enabled=enabled,
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    async def is_available(self) -> bool:
        """Return whether engram is usable right now.

        ``False`` when disabled (``enabled=False``) or unreachable. Never
        raises — call it to gate memory-dependent paths and degrade gracefully.
        """
        if not self.enabled:
            return False
        try:
            data = await self.health()
            return isinstance(data, dict) and data.get("status") == "ok"
        except Exception:
            return False

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

    def _search_namespaces(self) -> list[str] | None:
        """Namespaces to send on search, or ``None`` to let the server resolve
        from the principal's read permissions."""
        if self.read_namespaces is None:
            return None
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

    async def _resolve_user_id(self, scope: str | None = None) -> str:
        """Return the partition ``user_id`` for the given scope.

        Conventions match the MCP bridge's ``resolve_partition``:

        - ``shared`` → ``"global"`` (one cross-fleet partition)
        - ``machine`` → local short hostname (host-local partition)
        - ``user`` / ``project`` / ``inbox`` (default) → principal name from
          ``/whoami``, cached. Override via the constructor's ``user_id``.
        """
        if scope == "shared":
            return "global"
        if scope == "machine":
            return socket.gethostname().split(".")[0].lower()
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

        When ``namespaces`` (call-level) and ``read_namespaces`` (constructor)
        are both unset, the server resolves namespaces from the principal's
        read permissions — for an admin/wildcard token this spans every
        namespace. Pass ``namespaces=[...]`` to narrow per call.

        Project filter: pass ``project=""`` to search rows where
        ``project IS NULL`` (i.e. ``scope=shared``/``machine``/``user`` rows
        that don't carry a project). Pass ``project="<name>"`` to filter on
        that project. Omit to use the constructor default.
        """
        final_project = project if project is not None else self.project
        body: dict = {
            "query": query,
            "scope": scope or self.scope,
            "user_id": await self._resolve_user_id(scope or self.scope),
            "limit": limit,
        }
        if final_project:
            body["project"] = final_project
        ns = namespaces if namespaces is not None else self._search_namespaces()
        if ns is not None:
            body["namespaces"] = ns
        data = await self._request("POST", "/memory/search", json=body)
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
        expiration_days: int = 0,
    ) -> str:
        """Store or update a memory. Returns the key.

        ``expiration_days=0`` (default) means the memory never expires —
        engram is a durable store; curate/delete deliberately. Pass a
        positive value only for genuinely ephemeral memories (e.g. a
        time-boxed reminder).
        """
        data = await self._request(
            "POST",
            "/memory/set",
            json={
                "namespace": namespace or self.namespace,
                "key": key,
                "value": value,
                "scope": scope or self.scope,
                "user_id": await self._resolve_user_id(scope or self.scope),
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
                "user_id": await self._resolve_user_id(scope or self.scope),
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
                "user_id": await self._resolve_user_id(scope or self.scope),
                "project": project or self.project,
            },
        )
        return data.get("status") == "ok"

    async def whoami(self) -> dict:
        """Return the authenticated principal record. Triggers user_id cache."""
        return await self._request("GET", "/whoami")

    async def namespaces(self) -> dict:
        """Return the namespaces this token can read/write, with wildcards
        expanded server-side to concrete namespaces. Lets an app show a user
        what their assistant can recall. Shape: ``{"status", "read", "write"}``."""
        return await self._request("GET", "/namespaces")

    async def _resolve_reader_identity(self) -> str | None:
        """Return the inbox identity for this client. Order: explicit
        constructor arg > principal name from ``/whoami`` (cached) > None.

        An app authenticating with a human's token gets the human's name
        as its address (e.g. ``ixanadu``) — anyone in the fleet can reach
        the human there. A CC session that wants a per-project identity
        (``engram@macmini``) sets ``reader_identity=`` explicitly at
        construction time.
        """
        if self.reader_identity:
            return self.reader_identity
        name = await self._resolve_user_id(scope=None)
        return name if name and name != "unknown" else None

    # --- Inbox: inter-session messaging ----------------------------------

    async def inbox_send(
        self,
        to: str,
        body: str,
        *,
        subject: str = "",
        from_: str | None = None,
        thread_id: str | None = None,
    ) -> dict:
        """Send an inbox message addressed to a project or machine.

        ``from_`` defaults to ``self.reader_identity``. Returns the full
        server response (``status``, ``id``, ``guidance``, ...).
        """
        payload: dict = {"to": to, "body": body, "subject": subject}
        sender = from_ if from_ is not None else await self._resolve_reader_identity()
        if sender:
            payload["from_"] = sender
        if thread_id:
            payload["thread_id"] = thread_id
        return await self._request("POST", "/memory/send", json=payload)

    async def inbox_list(
        self,
        *,
        listen_set: list[str] | None = None,
        reader_identity: str | None = None,
        unread_only: bool = True,
        limit: int = 20,
    ) -> dict:
        """List inbox messages for ``listen_set``. Defaults to listening on
        ``[self.reader_identity]`` when both args are omitted.
        """
        reader = reader_identity if reader_identity is not None else await self._resolve_reader_identity()
        if listen_set is None:
            if not reader:
                raise ValueError(
                    "inbox_list needs listen_set or reader_identity (or a "
                    "reader_identity configured on the client)"
                )
            listen_set = [reader]
        return await self._request(
            "POST",
            "/memory/inbox",
            json={
                "listen_set": listen_set,
                "reader_identity": reader,
                "unread_only": unread_only,
                "limit": limit,
            },
        )

    async def inbox_ack(
        self,
        message_id: str,
        *,
        reader_identity: str | None = None,
    ) -> dict:
        """Mark an inbox message as read by ``reader_identity``."""
        reader = reader_identity if reader_identity is not None else await self._resolve_reader_identity()
        if not reader:
            raise ValueError("inbox_ack needs reader_identity")
        return await self._request(
            "POST",
            f"/memory/inbox/{message_id}/ack",
            json={"reader_identity": reader},
        )

    async def inbox_archive(
        self,
        message_id: str,
        *,
        reader_identity: str | None = None,
    ) -> dict:
        """Archive an inbox message (hide from all future inbox queries)."""
        reader = reader_identity if reader_identity is not None else await self._resolve_reader_identity()
        if not reader:
            raise ValueError("inbox_archive needs reader_identity")
        return await self._request(
            "POST",
            f"/memory/inbox/{message_id}/archive",
            json={"reader_identity": reader},
        )

    async def inbox_reply(
        self,
        parent: dict,
        body: str,
        *,
        subject: str = "",
        reader_identity: str | None = None,
    ) -> dict:
        """Reply to an inbox message: sends to the parent's sender, thread-
        links automatically, and acks the parent. ``parent`` is a message
        dict from ``inbox_list``. Returns the send response.
        """
        reader = (
            reader_identity
            if reader_identity is not None
            else await self._resolve_reader_identity()
        )
        parent_from = parent.get("from_") or parent.get("from")
        if not parent_from:
            raise ValueError("parent message has no 'from_' to reply to")
        # Strip @host so the reply lands on the sender's project address,
        # matching MCP memory_reply behavior.
        to = parent_from.split("@", 1)[0] if "@" in parent_from else parent_from
        thread_id = parent.get("thread_id") or parent.get("id")
        send = await self.inbox_send(
            to=to,
            body=body,
            subject=subject,
            from_=reader,
            thread_id=thread_id,
        )
        if reader:
            try:
                await self.inbox_ack(parent["id"], reader_identity=reader)
            except Exception:
                pass
        return send

    async def health(self) -> dict:
        """Check engram server health."""
        return await self._request("GET", "/health")

    async def close(self):
        """Close the underlying HTTP connection pool."""
        if self._client:
            await self._client.aclose()
            self._client = None
