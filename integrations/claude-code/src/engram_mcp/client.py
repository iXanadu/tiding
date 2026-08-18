import asyncio
import socket

import httpx

from engram_mcp.identity import derive_project_name, remember_project_dir
from engram_mcp.model import resolve_model


class MemoryClient:
    """Async HTTP client for the engram semantic memory REST API.

    Uses a persistent httpx.AsyncClient to reuse TCP connections across calls,
    avoiding connection setup overhead when multiple calls fire in parallel.
    """

    def __init__(self, base_url: str = "http://localhost:8920", api_token: str = ""):
        self.base_url = base_url.rstrip("/")
        headers = {"X-Engram-Machine": socket.gethostname().split(".")[0].lower()}
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=headers, timeout=30.0,
        )

    def _provenance_headers(self, project_dir: str | None) -> dict:
        """Per-request headers identifying which folder the caller is in.

        Server stores these into ``metadata.project`` / ``metadata.cwd`` on
        memory rows so the dashboard can filter by origin.

        Resolve through the session anchor (``remember_project_dir``) so an
        omitted ``project_dir`` reports the session's REAL project — via the
        startup-cwd anchor / ``.engram.cfg`` — instead of collapsing to
        ``admin`` / empty. Identity resolution already ran (and set the pin)
        before this call, so this only reads the effective directory.
        """
        effective = remember_project_dir(project_dir)
        headers = {
            "X-Engram-Project": derive_project_name(effective),
            "X-Engram-Cwd": (effective or "").strip(),
        }
        # MODEL-RECORD-1. Which MODEL did this, read from the harness's own
        # record — never asked of the agent (no tokens, not self-attestation)
        # and re-read per call, because the model can change mid-session.
        # `provider` cannot answer this any more: it names the HARNESS, and a
        # harness is no longer one model.
        #
        # The source travels WITH the value and is sent even when the model is
        # unknown. Without it, a blank field is ambiguous between "ran an
        # unrecorded model" and "nobody looked" — the exact half-populated
        # provenance field found on a peer's wire the same day this shipped.
        model, source = resolve_model()
        headers["X-Engram-Model-Source"] = source
        if model:
            headers["X-Engram-Model"] = model
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """Send a request with one retry on transient connection/timeout errors."""
        for attempt in range(2):
            try:
                resp = await self._client.request(method, path, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise

    async def store(
        self,
        key: str,
        value: str,
        namespace: str,
        scope: str,
        user_id: str,
        project: str | None = None,
        tags: str = "",
        project_dir: str | None = None,
        listen_set: list[str] | None = None,
        reader_identity: str | None = None,
        if_match: str | None = None,
    ) -> dict:
        body: dict = {
            "namespace": namespace,
            "key": key,
            "value": value,
            "scope": scope,
            "user_id": user_id,
            "tags": tags,
        }
        if project is not None:
            body["project"] = project
        if listen_set:
            body["listen_set"] = listen_set
        if reader_identity:
            body["reader_identity"] = reader_identity
        if if_match is not None:
            # MEM-4: make the write conditional on the value being unchanged
            # since we read it. Empty string asserts the key is unused.
            body["if_match"] = if_match
        return await self._request(
            "POST",
            "/memory/set",
            json=body,
            headers=self._provenance_headers(project_dir),
        )

    async def get(
        self,
        key: str,
        namespace: str,
        scope: str,
        user_id: str,
        project: str | None = None,
        project_dir: str | None = None,
    ) -> dict:
        body: dict = {
            "namespace": namespace,
            "key": key,
            "scope": scope,
            "user_id": user_id,
        }
        if project is not None:
            body["project"] = project
        return await self._request(
            "POST",
            "/memory/get",
            json=body,
            headers=self._provenance_headers(project_dir),
        )

    async def search(
        self,
        query: str,
        scope: str,
        user_id: str,
        project: str | None = None,
        limit: int = 5,
        namespace: str | None = None,
        namespaces: list[str] | None = None,
        listen_set: list[str] | None = None,
        reader_identity: str | None = None,
        project_dir: str | None = None,
        snippet_lines: int | None = None,
    ) -> dict:
        body: dict = {
            "query": query,
            "scope": scope,
            "user_id": user_id,
            "limit": limit,
        }
        if snippet_lines:
            body["snippet_lines"] = snippet_lines
        if project is not None:
            body["project"] = project
        if namespaces:
            body["namespaces"] = namespaces
        elif namespace:
            body["namespace"] = namespace
        if listen_set:
            body["listen_set"] = listen_set
        if reader_identity:
            body["reader_identity"] = reader_identity
        return await self._request(
            "POST",
            "/memory/search",
            json=body,
            headers=self._provenance_headers(project_dir),
        )

    async def keys(
        self,
        prefix: str,
        scope: str,
        user_id: str,
        project: str | None = None,
        limit: int = 500,
        namespaces: list[str] | None = None,
        project_dir: str | None = None,
    ) -> dict:
        """Deterministic key enumeration under a prefix (MEM-2). No embedding;
        key-ordered; response carries `total` so truncation is legible."""
        body: dict = {
            "prefix": prefix,
            "scope": scope,
            "user_id": user_id,
            "limit": limit,
        }
        if project is not None:
            body["project"] = project
        if namespaces:
            body["namespaces"] = namespaces
        return await self._request(
            "POST",
            "/memory/keys",
            json=body,
            headers=self._provenance_headers(project_dir),
        )

    async def whoami(self) -> dict:
        """Return the authenticated principal record. Requires a valid
        bearer token; returns 401 if anonymous."""
        return await self._request("GET", "/whoami")

    async def namespaces(self) -> dict:
        """Return the namespaces the caller can read/write, with wildcards
        expanded server-side to concrete namespaces. Requires a valid token.
        Shape: ``{"status": "ok", "read": [...], "write": [...]}``."""
        return await self._request("GET", "/namespaces")

    async def wake_poll(
        self,
        listen_set: list[str],
        reader_identity: str | None = None,
        since: str | None = None,
    ) -> dict:
        """Band D 10a: one non-blocking fetch of unexpired wake rows for the
        listen_set. The server never pops — every live watcher on a shared
        address sees the wake and dedupes by id, exactly like mail `seen`."""
        payload: dict = {"listen_set": listen_set, "timeout_seconds": 0}
        if reader_identity:
            payload["reader_identity"] = reader_identity
        if since:
            payload["since"] = since
        return await self._request("POST", "/memory/wake/poll", json=payload)

    async def session_addresses(self, project: str | None = None) -> dict:
        """ADDR-REG: every name the store holds (optionally one project's
        subtree), each with its mail count, liveness facts, and why the
        allocator would skip it. Shape: ``{"status": "ok", "entries": [...]}``."""
        params = {"project": project} if project else None
        return await self._request("GET", "/session/addresses", params=params)

    async def inbox_send(
        self,
        to: str | list[str],
        body: str,
        subject: str = "",
        from_: str | None = None,
        thread_id: str | None = None,
        project_dir: str | None = None,
        intent: str | None = None,
        supersedes: str | None = None,
        listen_set: list[str] | None = None,
        from_lane: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict:
        payload: dict = {"to": to, "body": body, "subject": subject}
        if in_reply_to:
            # Step 12: the id this message ANSWERS — what makes "a reply to
            # that ask" recoverable for the handled discriminator.
            payload["in_reply_to"] = in_reply_to
        if from_lane:
            # LANE-5: the sender's immortal lane — recipients' replies route
            # here so they survive this session's death. Empty = legacy routing.
            payload["from_lane"] = from_lane
        if from_:
            payload["from_"] = from_
        if thread_id:
            payload["thread_id"] = thread_id
        if intent:
            payload["intent"] = intent
        if supersedes:
            payload["supersedes"] = supersedes
        if listen_set:
            # ADDR-1: the server cannot reconstruct this from from_ once a
            # session holds a seat — the identity string carries neither the
            # project group address nor channel subscriptions.
            payload["listen_set"] = listen_set
        return await self._request(
            "POST",
            "/memory/send",
            json=payload,
            headers=self._provenance_headers(project_dir),
        )

    async def presence_update(
        self,
        identity: str,
        project: str,
        state: str = "running",
        provider: str | None = None,
        overlays: list[str] | None = None,
        channels: list[str] | None = None,
        session_nonce: str | None = None,
        project_dir: str | None = None,
        watcher: bool = False,
        host: str | None = None,
    ) -> dict:
        """Self-reported liveness heartbeat (MSG-4).

        ``watcher=True`` marks the beat as coming from the inbox watcher
        rather than the session: it records that an EAR is alive at this
        address (MSG-5) and refreshes liveness (SEAT-7) without touching the
        state the session reported.

        ``host`` (PRES-2) stamps the box this beat comes from — the server
        cannot derive it, and the seat-exempt admin role has no seat row to
        join one from.
        """
        return await self._request(
            "POST",
            "/memory/presence",
            json={
                "identity": identity,
                "project": project,
                "state": state,
                "provider": provider,
                "overlays": overlays or [],
                "channels": channels or [],
                "session_nonce": session_nonce,
                "watcher": watcher,
                "host": host,
            },
            headers=self._provenance_headers(project_dir),
        )

    async def presence_farewell(
        self,
        identity: str,
        project: str,
        project_dir: str | None = None,
    ) -> dict:
        """Report that the watched SESSION process is gone.

        Sent by the watcher, which OUTLIVES its session and therefore observes
        the death rather than announcing its own. A dying process is a poor
        reporter — it may never be scheduled, and SIGKILL gives it nothing to
        say — so the farewell is an observation, not a last gasp.

        Only ever sent for an observed transition alive → gone. A watcher that
        is killed itself sends nothing, and that silence is correct: absence of
        a farewell means nothing at all.
        """
        return await self._request(
            "POST",
            "/memory/presence",
            json={
                "identity": identity,
                "project": project,
                "state": "running",  # ignored on the farewell path
                "farewell": True,
            },
            headers=self._provenance_headers(project_dir),
        )

    async def session_claim(
        self,
        session_key: str,
        project: str,
        provider: str = "claude",
        session_nonce: str | None = None,
        host: str | None = None,
        preferred_seat: str | None = None,
        project_dir: str | None = None,
        runtime_seat: bool = False,
    ) -> dict:
        """Claim this session's unique inbox address (SEAT-3).

        Idempotent on ``session_key`` — safe to call on every heartbeat.
        ``runtime_seat=True`` (ID-2) tells the server the preferred seat was
        taken deliberately mid-session, so continuity should MOVE the
        registration to it rather than answer with the seat already held.
        """
        return await self._request(
            "POST",
            "/session/claim",
            json={
                "session_key": session_key,
                "project": project,
                "provider": provider,
                "session_nonce": session_nonce,
                "host": host,
                "preferred_seat": preferred_seat,
                "runtime_seat": runtime_seat,
            },
            headers=self._provenance_headers(project_dir),
        )

    async def session_release(
        self,
        session_key: str,
        project: str,
        project_dir: str | None = None,
    ) -> dict:
        """Free this session's seat immediately (SEAT-3)."""
        return await self._request(
            "POST",
            "/session/release",
            json={"session_key": session_key, "project": project},
            headers=self._provenance_headers(project_dir),
        )

    async def roster(
        self,
        project: str | None = None,
        channel: str | None = None,
        include_done: bool = False,
        project_dir: str | None = None,
    ) -> dict:
        """Who is live on a project / #channel / the whole box (MSG-4)."""
        return await self._request(
            "POST",
            "/memory/roster",
            json={
                "project": project,
                "channel": channel,
                "include_done": include_done,
            },
            headers=self._provenance_headers(project_dir),
        )

    async def inbox_list(
        self,
        listen_set: list[str],
        reader_identity: str | None = None,
        unread_only: bool = True,
        limit: int = 20,
        project_dir: str | None = None,
        newest_first: bool = False,
        include_resolved: bool = False,
    ) -> dict:
        return await self._request(
            "POST",
            "/memory/inbox",
            json={
                "listen_set": listen_set,
                "reader_identity": reader_identity,
                "unread_only": unread_only,
                "limit": limit,
                "newest_first": newest_first,
                "include_resolved": include_resolved,
            },
            headers=self._provenance_headers(project_dir),
        )

    async def inbox_ack(
        self,
        message_id: str,
        reader_identity: str,
        project_dir: str | None = None,
    ) -> dict:
        return await self._request(
            "POST",
            f"/memory/inbox/{message_id}/ack",
            json={"reader_identity": reader_identity},
            headers=self._provenance_headers(project_dir),
        )

    async def inbox_archive(
        self,
        message_id: str,
        reader_identity: str,
        project_dir: str | None = None,
    ) -> dict:
        return await self._request(
            "POST",
            f"/memory/inbox/{message_id}/archive",
            json={"reader_identity": reader_identity},
            headers=self._provenance_headers(project_dir),
        )

    async def inbox_resolve(
        self,
        message_id: str,
        reader_identity: str,
        project_dir: str | None = None,
    ) -> dict:
        return await self._request(
            "POST",
            f"/memory/inbox/{message_id}/resolve",
            json={"reader_identity": reader_identity},
            headers=self._provenance_headers(project_dir),
        )

    async def inbox_resolve_thread(
        self,
        thread_id: str,
        listen_set: list[str],
        reader_identity: str | None = None,
        project_dir: str | None = None,
    ) -> dict:
        return await self._request(
            "POST",
            "/memory/inbox/resolve-thread",
            json={
                "thread_id": thread_id,
                "listen_set": listen_set,
                "reader_identity": reader_identity,
            },
            headers=self._provenance_headers(project_dir),
        )

    async def supersede(
        self,
        key: str,
        namespace: str,
        project: str | None,
        target_user_id: str,
        reason: str,
        replacement_key: str | None = None,
        project_dir: str | None = None,
        scope: str = "project",
    ) -> dict:
        body: dict = {
            "namespace": namespace,
            "key": key,
            "scope": scope,
            "project": project,
            "target_user_id": target_user_id,
            "reason": reason,
        }
        if replacement_key:
            body["replacement_key"] = replacement_key
        return await self._request(
            "POST", "/memory/supersede", json=body,
            headers=self._provenance_headers(project_dir),
        )

    async def flag_deletion(
        self,
        key: str,
        namespace: str,
        scope: str,
        user_id: str,
        reason: str,
        project: str | None = None,
        project_dir: str | None = None,
    ) -> dict:
        body: dict = {
            "namespace": namespace,
            "key": key,
            "scope": scope,
            "user_id": user_id,
            "reason": reason,
        }
        if project is not None:
            body["project"] = project
        return await self._request(
            "POST", "/memory/flag_deletion", json=body,
            headers=self._provenance_headers(project_dir),
        )

    async def forget(
        self,
        key: str,
        namespace: str,
        scope: str,
        user_id: str,
        project: str | None = None,
        project_dir: str | None = None,
    ) -> dict:
        body: dict = {
            "namespace": namespace,
            "key": key,
            "scope": scope,
            "user_id": user_id,
        }
        if project is not None:
            body["project"] = project
        return await self._request(
            "POST",
            "/memory/forget",
            json=body,
            headers=self._provenance_headers(project_dir),
        )

    async def health(self) -> dict:
        return await self._request("GET", "/health", timeout=10.0)

    async def close(self):
        await self._client.aclose()
