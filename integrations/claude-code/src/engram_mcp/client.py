import asyncio
import socket

import httpx

from engram_mcp.identity import derive_project_name


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
        """
        return {
            "X-Engram-Project": derive_project_name(project_dir),
            "X-Engram-Cwd": (project_dir or "").strip(),
        }

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
    ) -> dict:
        body: dict = {
            "query": query,
            "scope": scope,
            "user_id": user_id,
            "limit": limit,
        }
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

    async def whoami(self) -> dict:
        """Return the authenticated principal record. Requires a valid
        bearer token; returns 401 if anonymous."""
        return await self._request("GET", "/whoami")

    async def namespaces(self) -> dict:
        """Return the namespaces the caller can read/write, with wildcards
        expanded server-side to concrete namespaces. Requires a valid token.
        Shape: ``{"status": "ok", "read": [...], "write": [...]}``."""
        return await self._request("GET", "/namespaces")

    async def inbox_send(
        self,
        to: str,
        body: str,
        subject: str = "",
        from_: str | None = None,
        thread_id: str | None = None,
        project_dir: str | None = None,
    ) -> dict:
        payload: dict = {"to": to, "body": body, "subject": subject}
        if from_:
            payload["from_"] = from_
        if thread_id:
            payload["thread_id"] = thread_id
        return await self._request(
            "POST",
            "/memory/send",
            json=payload,
            headers=self._provenance_headers(project_dir),
        )

    async def inbox_list(
        self,
        listen_set: list[str],
        reader_identity: str | None = None,
        unread_only: bool = True,
        limit: int = 20,
        project_dir: str | None = None,
    ) -> dict:
        return await self._request(
            "POST",
            "/memory/inbox",
            json={
                "listen_set": listen_set,
                "reader_identity": reader_identity,
                "unread_only": unread_only,
                "limit": limit,
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
