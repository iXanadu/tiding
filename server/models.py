from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator


# --- Request models (match original Pyscript tool interface) ---

class MemorySetRequest(BaseModel):
    namespace: str
    key: str
    value: str
    scope: str = "user"
    user_id: str = "default"
    tags: str = ""
    tags_search: str = ""
    expiration_days: int = 180
    force_new: bool = False
    listen_set: list[str] | None = None
    reader_identity: str | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def coerce_tags(cls, v):
        if isinstance(v, list):
            return ", ".join(str(t) for t in v)
        return v


class MemoryGetRequest(BaseModel):
    namespace: str
    key: str
    scope: str = "user"
    user_id: str = "default"


class MemorySearchRequest(BaseModel):
    namespace: str | None = None
    namespaces: list[str] | None = None
    query: str
    scope: str = "user"
    user_id: str = "default"
    limit: int = 5
    listen_set: list[str] | None = None
    reader_identity: str | None = None

    @model_validator(mode="after")
    def require_namespace(self):
        if not self.namespace and not self.namespaces:
            raise ValueError("Either 'namespace' or 'namespaces' is required")
        return self

    @field_validator("query", mode="before")
    @classmethod
    def query_not_empty(cls, v):
        if isinstance(v, str) and not v.strip():
            raise ValueError("query must not be empty")
        return v

    def resolved_namespaces(self) -> list[str]:
        """Return the list of namespaces to search (normalizes both fields)."""
        if self.namespaces:
            return self.namespaces
        return [self.namespace]


class MemoryForgetRequest(BaseModel):
    namespace: str
    key: str
    scope: str = "user"
    user_id: str = "default"


# --- Response models ---

class MemoryItem(BaseModel):
    namespace: str
    key: str
    value: str
    scope: str
    user_id: str = "default"
    tags: str
    tags_search: str
    score: float | None = None


class InboxBanner(BaseModel):
    unread_count: int
    preview: list[str]  # ≤5 one-line previews of unread messages


class MemorySetResponse(BaseModel):
    status: str
    key: str
    inbox_banner: InboxBanner | None = None


class MemoryGetResponse(BaseModel):
    status: str
    memory: MemoryItem | None = None


class MemorySearchResponse(BaseModel):
    status: str
    results: list[MemoryItem]
    inbox_banner: InboxBanner | None = None


class MemoryForgetResponse(BaseModel):
    status: str
    key: str


# --- Admin models ---

class MemoryListItem(BaseModel):
    namespace: str
    key: str
    value: str | None = None
    scope: str
    user_id: str
    tags: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime | None = None
    metadata: dict | None = None
    owner: str | None = None


class MemoryUpdateRequest(BaseModel):
    namespace: str
    key: str
    scope: str
    user_id: str
    new_namespace: str | None = None
    new_scope: str | None = None
    new_user_id: str | None = None
    new_key: str | None = None
    new_tags: str | None = None


class MemoryUpdateResponse(BaseModel):
    status: str


class MemoryListResponse(BaseModel):
    status: str
    total: int
    offset: int
    limit: int
    items: list[MemoryListItem]


class NamespaceStats(BaseModel):
    namespace: str
    scope: str | None = None
    count: int
    oldest: datetime | None = None
    newest: datetime | None = None
    expired_count: int = 0


class MemoryStatsResponse(BaseModel):
    status: str
    stats: list[NamespaceStats]


class BulkDeleteRequest(BaseModel):
    namespace: str
    key_prefix: str
    scope: str | None = None
    user_id: str | None = None
    older_than_days: int | None = None

    @field_validator("key_prefix", mode="before")
    @classmethod
    def key_prefix_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("key_prefix must not be empty")
        return v


class BulkDeleteResponse(BaseModel):
    status: str
    deleted_count: int


class CleanupResponse(BaseModel):
    status: str
    deleted_count: int


# --- Principal models ---

class PrincipalCreate(BaseModel):
    name: str
    type: str
    is_admin: bool = False
    password: str | None = None
    token: str | None = None
    read_namespaces: list[str] | None = None
    write_namespaces: list[str] | None = None

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("name must not be empty")
        return v.strip().lower()

    @field_validator("type", mode="before")
    @classmethod
    def validate_type(cls, v):
        if v not in ("human", "agent"):
            raise ValueError("type must be 'human' or 'agent'")
        return v


class PrincipalUpdate(BaseModel):
    is_admin: bool | None = None
    password: str | None = None
    token: str | None = None
    read_namespaces: list[str] | None = None
    write_namespaces: list[str] | None = None
    active: bool | None = None


class PrincipalResponse(BaseModel):
    id: str
    name: str
    type: str
    is_admin: bool
    has_token: bool
    has_password: bool
    read_namespaces: list[str]
    write_namespaces: list[str]
    active: bool
    created_at: datetime


class PrincipalListResponse(BaseModel):
    status: str
    principals: list[PrincipalResponse]


class PrincipalCreateResponse(BaseModel):
    status: str
    principal: PrincipalResponse
    raw_token: str | None = None


class AliasCreate(BaseModel):
    alias: str
    source: str

    @field_validator("alias", mode="before")
    @classmethod
    def alias_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("alias must not be empty")
        return v.strip()

    @field_validator("source", mode="before")
    @classmethod
    def source_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("source must not be empty")
        return v.strip()


class AliasResponse(BaseModel):
    id: str
    principal_id: str
    alias: str
    source: str


class TokenResponse(BaseModel):
    status: str
    principal_name: str
    raw_token: str


# --- Inbox models (inter-session messaging on top of the memories table) ---

class InboxSendRequest(BaseModel):
    to: str
    body: str
    subject: str = ""
    from_: str | None = None  # sender identity — MCP bridge stamps this
    thread_id: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("to", mode="before")
    @classmethod
    def to_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("'to' must be a non-empty string")
        return v.strip()

    @field_validator("body", mode="before")
    @classmethod
    def body_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("'body' must be a non-empty string")
        return v


class InboxMessage(BaseModel):
    id: str  # memory key, e.g. "inbox/<uuid>"
    to: str
    from_: str | None = None
    subject: str
    body: str
    thread_id: str | None = None
    read_by: list[str]
    archived: bool
    created_at: datetime

    model_config = {"populate_by_name": True}


class InboxSendResponse(BaseModel):
    status: str
    id: str
    corrected_from: str | None = None
    guidance: str | None = None


class InboxListRequest(BaseModel):
    listen_set: list[str]
    reader_identity: str | None = None
    unread_only: bool = True
    limit: int = 20


class InboxListResponse(BaseModel):
    status: str
    messages: list[InboxMessage]
    guidance: str | None = None


class InboxAckRequest(BaseModel):
    reader_identity: str

    @field_validator("reader_identity", mode="before")
    @classmethod
    def reader_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("'reader_identity' must be a non-empty string")
        return v.strip()


class InboxAckResponse(BaseModel):
    status: str
    id: str
    guidance: str | None = None
