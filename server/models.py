from datetime import datetime

from pydantic import BaseModel, field_validator

from server.config import settings


def _canon_ns(v):
    """NS-1: canonicalize legacy namespace names at the API boundary so a
    rename is a relabel, never a breaking change (old clients keep working)."""
    if isinstance(v, str):
        return settings.canonical_namespace(v)
    if isinstance(v, list):
        return [settings.canonical_namespace(x) if isinstance(x, str) else x for x in v]
    return v


class _NamespacedRequest(BaseModel):
    """Mixin: any request model with namespace/namespaces fields gets alias
    canonicalization automatically."""

    @field_validator("namespace", "namespaces", mode="before", check_fields=False)
    @classmethod
    def _canonicalize_namespace(cls, v):
        return _canon_ns(v)


# --- Request models (match original Pyscript tool interface) ---

class MemorySetRequest(_NamespacedRequest):
    namespace: str
    key: str
    value: str
    scope: str = "user"
    user_id: str = "default"
    project: str | None = None
    tags: str = ""
    tags_search: str = ""
    expiration_days: int = 0  # 0 = never expires (permanent default); set a positive TTL for ephemeral memories
    force_new: bool = False
    listen_set: list[str] | None = None
    reader_identity: str | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def coerce_tags(cls, v):
        if isinstance(v, list):
            return ", ".join(str(t) for t in v)
        return v


class MemoryGetRequest(_NamespacedRequest):
    namespace: str
    key: str
    scope: str = "user"
    user_id: str = "default"
    project: str | None = None


class MemorySearchRequest(_NamespacedRequest):
    namespace: str | None = None
    namespaces: list[str] | None = None
    query: str
    scope: str = "user"
    user_id: str = "default"
    project: str | None = None
    limit: int = 5
    listen_set: list[str] | None = None
    reader_identity: str | None = None

    @field_validator("query", mode="before")
    @classmethod
    def query_not_empty(cls, v):
        if isinstance(v, str) and not v.strip():
            raise ValueError("query must not be empty")
        return v

    def explicit_namespaces(self) -> list[str] | None:
        """Return the namespaces the client explicitly asked for, or None
        if both fields were omitted (server should resolve from the principal)."""
        if self.namespaces:
            return self.namespaces
        if self.namespace:
            return [self.namespace]
        return None


class MemoryForgetRequest(_NamespacedRequest):
    namespace: str
    key: str
    scope: str = "user"
    user_id: str = "default"
    project: str | None = None


# --- Response models ---

class MemoryItem(BaseModel):
    namespace: str
    key: str
    value: str
    scope: str
    user_id: str = "default"
    project: str | None = None
    tags: str
    tags_search: str
    score: float | None = None
    created_at: datetime | None = None


class InboxBanner(BaseModel):
    unread_count: int
    preview: list[str]  # ≤5 one-line previews of unread messages


class MemorySetResponse(BaseModel):
    status: str
    key: str
    # The CANONICAL namespace the write landed in (request namespaces are
    # alias-canonicalized at the boundary) — lets clients display the truth
    # instead of echoing their possibly-legacy configured name.
    namespace: str | None = None
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
    project: str | None = None
    tags: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime | None = None
    metadata: dict | None = None
    owner: str | None = None


class MemoryUpdateRequest(_NamespacedRequest):
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


class BulkDeleteRequest(_NamespacedRequest):
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


class NamespacesResponse(BaseModel):
    """Caller-scoped namespace list. Wildcards in the principal's permissions
    are expanded server-side to concrete namespaces present in the DB."""
    status: str
    read: list[str]
    write: list[str]


# --- Inbox models (inter-session messaging on top of the memories table) ---

# Message intent — drives wake policy (action/authority-directive wake a dormant
# agent; fyi does not) and the drive vocabulary (proceed/escalate). None = legacy,
# treated as waking for back-compat with pre-intent senders.
INBOX_INTENTS = {"fyi", "action", "proceed", "escalate", "authority-directive"}


class InboxSendRequest(BaseModel):
    # A single address, or a list for ad-hoc multi-recipient fan-out
    # (each recipient gets their own message row / id).
    to: str | list[str]
    body: str
    subject: str = ""
    from_: str | None = None  # sender identity — MCP bridge stamps this
    thread_id: str | None = None
    supersedes: str | None = None  # id of a prior message this one replaces
    intent: str | None = None  # fyi | action | proceed | escalate | authority-directive

    model_config = {"populate_by_name": True}

    @field_validator("intent")
    @classmethod
    def intent_valid(cls, v):
        if v is not None and v not in INBOX_INTENTS:
            raise ValueError(
                f"invalid intent '{v}'; allowed: {sorted(INBOX_INTENTS)} or omit"
            )
        return v

    @field_validator("to", mode="before")
    @classmethod
    def to_not_empty(cls, v):
        if isinstance(v, str):
            if not v.strip():
                raise ValueError("'to' must be a non-empty string")
            return v.strip()
        if isinstance(v, list):
            if not v or not all(isinstance(a, str) and a.strip() for a in v):
                raise ValueError("'to' list must be non-empty strings")
            # dedupe, preserve order
            seen, out = set(), []
            for a in v:
                a = a.strip()
                if a.lower() not in seen:
                    seen.add(a.lower())
                    out.append(a)
            return out
        raise ValueError("'to' must be a string or list of strings")

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
    # Server-verified provenance (MSG-1/MSG-2): `from_` above is the sender's
    # self-asserted label; these two are derived from the authenticated token
    # and cannot be spoofed by the client.
    from_principal: str | None = None  # which principal actually sent it (None = unverified/legacy/anon)
    authority: bool = False            # the sending principal is an owner (is_admin)
    intent: str | None = None          # fyi | action | proceed | escalate | authority-directive
    subject: str
    body: str
    thread_id: str | None = None
    read_by: list[str]
    archived: bool
    created_at: datetime
    # Lifecycle (coordination ≠ knowledge): a message drains as it is handled.
    status: str = "open"  # open | resolved | superseded
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    supersedes: str | None = None  # id this message replaces
    superseded_by: str | None = None  # id of the message that replaced this one
    # Read-side staleness: annotated, never auto-deleted.
    is_stale: bool = False
    age_hours: float | None = None

    model_config = {"populate_by_name": True}


class InboxSendResponse(BaseModel):
    status: str
    id: str                       # first (or only) message id — back-compat
    ids: list[str] | None = None  # all ids when 'to' was a list (fan-out)
    corrected_from: str | None = None
    guidance: str | None = None


class InboxListRequest(BaseModel):
    listen_set: list[str]
    reader_identity: str | None = None
    unread_only: bool = True
    include_resolved: bool = False  # default view hides resolved/superseded
    limit: int = 20
    newest_first: bool = False  # watcher sets True so new mail never truncates out


class InboxListResponse(BaseModel):
    status: str
    messages: list[InboxMessage]
    guidance: str | None = None


# --- Inbox long-poll wait (any-harness wake primitive) --------------------

class InboxWaitRequest(BaseModel):
    """Block until new mail arrives (or timeout). Lets ANY harness — anything
    that can POST — implement wake-on-message without engram's watcher binary."""
    listen_set: list[str]
    reader_identity: str | None = None
    timeout_seconds: float = 30.0   # capped server-side
    since: datetime | None = None   # only mail newer than this; default = request arrival
    include_fyi: bool = False       # fyi is informational: excluded from wakes by default

    @field_validator("timeout_seconds")
    @classmethod
    def timeout_bounds(cls, v):
        if not (0 <= v <= 300):
            raise ValueError("timeout_seconds must be between 0 and 300")
        return v


class InboxWaitResponse(BaseModel):
    status: str                 # "ok" (messages) | "timeout" (none arrived)
    messages: list["InboxMessage"]
    waited_seconds: float
    guidance: str | None = None


# --- Presence / liveness roster (MSG-4) ---------------------------------

PRESENCE_STATES = {"running", "awaiting-input", "done"}


class PresenceUpdateRequest(BaseModel):
    """Self-reported liveness heartbeat. The harness POSTs its own state
    transitions (running → awaiting-input → done); engram never scrapes or
    infers state — last_seen is the only server-side fallback signal."""
    identity: str            # inbox identity, e.g. "projdelta-grok"
    project: str             # bare project name (the roster grouping key)
    state: str               # running | awaiting-input | done
    provider: str | None = None      # claude | grok | codex | ...
    overlays: list[str] = []         # e.g. ["projdelta/builders"]
    channels: list[str] = []         # e.g. ["#courseware"]
    # Per-PROCESS random nonce (SEAT collision detection): two live sessions
    # heartbeating one identity with different nonces = the silent "two bodies,
    # one seat" misconfiguration (shared acks, mutual self-echo drop). None =
    # legacy client; collision tracking skipped for that heartbeat.
    session_nonce: str | None = None

    @field_validator("identity", "project", mode="before")
    @classmethod
    def presence_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v.strip().lower()

    @field_validator("state")
    @classmethod
    def state_valid(cls, v):
        if v not in PRESENCE_STATES:
            raise ValueError(f"invalid state '{v}'; allowed: {sorted(PRESENCE_STATES)}")
        return v


class RosterEntry(BaseModel):
    identity: str
    project: str
    state: str
    provider: str | None = None
    overlays: list[str] = []
    channels: list[str] = []
    last_seen: datetime
    age_seconds: float
    is_stale: bool  # last_seen older than the staleness threshold
    # Seat-collision detection: number of distinct live sessions (fresh
    # nonces) heartbeating this one identity, and whether that's a flagged
    # collision (>1 on a non-exempt identity — see SEAT_EXEMPT_IDENTITIES).
    live_sessions: int = 1
    collision: bool = False
    providers_seen: list[str] = []  # providers across live sessions (collision detail)


class RosterRequest(BaseModel):
    project: str | None = None   # None = whole-box roster
    channel: str | None = None   # filter to members of a #channel
    include_done: bool = False   # done sessions hidden by default


class RosterResponse(BaseModel):
    status: str
    entries: list[RosterEntry]
    guidance: str | None = None


class PresenceUpdateResponse(BaseModel):
    status: str
    identity: str
    state: str
    # Set when this identity has >1 live session (fresh nonces) and is not an
    # exempt shared-role identity: {"live_sessions": n, "providers": [...]}.
    # Clients surface this LOUDLY — the second seat must declare a
    # discriminator (ENGRAM_INBOX_IDENTITY) or the sessions share ack-state
    # and cannot message each other.
    collision: dict | None = None


class InboxAckRequest(BaseModel):
    reader_identity: str

    @field_validator("reader_identity", mode="before")
    @classmethod
    def reader_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("'reader_identity' must be a non-empty string")
        return v.strip()


class InboxResolveRequest(BaseModel):
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
