import asyncio
import socket

import httpx


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
        tags: str = "",
    ) -> dict:
        return await self._request(
            "POST",
            "/memory/set",
            json={
                "namespace": namespace,
                "key": key,
                "value": value,
                "scope": scope,
                "user_id": user_id,
                "tags": tags,
            },
        )

    async def get(
        self,
        key: str,
        namespace: str,
        scope: str,
        user_id: str,
    ) -> dict:
        return await self._request(
            "POST",
            "/memory/get",
            json={
                "namespace": namespace,
                "key": key,
                "scope": scope,
                "user_id": user_id,
            },
        )

    async def search(
        self,
        query: str,
        scope: str,
        user_id: str,
        limit: int = 5,
        namespace: str | None = None,
        namespaces: list[str] | None = None,
        listen_set: list[str] | None = None,
        reader_identity: str | None = None,
    ) -> dict:
        body: dict = {
            "query": query,
            "scope": scope,
            "user_id": user_id,
            "limit": limit,
        }
        if namespaces:
            body["namespaces"] = namespaces
        elif namespace:
            body["namespace"] = namespace
        if listen_set:
            body["listen_set"] = listen_set
        if reader_identity:
            body["reader_identity"] = reader_identity
        return await self._request("POST", "/memory/search", json=body)

    async def inbox_send(
        self,
        to: str,
        body: str,
        subject: str = "",
        from_: str | None = None,
        thread_id: str | None = None,
    ) -> dict:
        payload: dict = {"to": to, "body": body, "subject": subject}
        if from_:
            payload["from_"] = from_
        if thread_id:
            payload["thread_id"] = thread_id
        return await self._request("POST", "/memory/send", json=payload)

    async def inbox_list(
        self,
        listen_set: list[str],
        reader_identity: str | None = None,
        unread_only: bool = True,
        limit: int = 20,
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
        )

    async def inbox_ack(self, message_id: str, reader_identity: str) -> dict:
        return await self._request(
            "POST",
            f"/memory/inbox/{message_id}/ack",
            json={"reader_identity": reader_identity},
        )

    async def inbox_archive(self, message_id: str, reader_identity: str) -> dict:
        return await self._request(
            "POST",
            f"/memory/inbox/{message_id}/archive",
            json={"reader_identity": reader_identity},
        )

    async def forget(
        self,
        key: str,
        namespace: str,
        scope: str,
        user_id: str,
    ) -> dict:
        return await self._request(
            "POST",
            "/memory/forget",
            json={
                "namespace": namespace,
                "key": key,
                "scope": scope,
                "user_id": user_id,
            },
        )

    async def health(self) -> dict:
        return await self._request("GET", "/health", timeout=10.0)

    async def close(self):
        await self._client.aclose()
