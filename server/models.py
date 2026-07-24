from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from server.config import settings

# Input-size caps (anti-DoS: an oversized value forces a costly embedding on
# every write; unbounded limits force full scans). Generous for real use.
MAX_VALUE = 256_000       # ~256 KB of memory/message text
MAX_KEY = 512
MAX_TAGS = 4_000
MAX_SUBJECT = 1_000
MAX_ADDR = 256            # one address / identity / project name
MAX_LIST = 64             # listen_set / channels / overlays element count
MAX_SEARCH_LIMIT = 200
MAX_INBOX_LIMIT = 500
MAX_EXPIRY_DAYS = 36_500  # ~100 years


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
    key: str = Field(max_length=MAX_KEY)
    value: str = Field(max_length=MAX_VALUE)
    scope: str = "user"
    user_id: str = Field(default="default", max_length=MAX_ADDR)
    project: str | None = Field(default=None, max_length=MAX_ADDR)
    tags: str = Field(default="", max_length=MAX_TAGS)
    tags_search: str = Field(default="", max_length=MAX_TAGS)
    expiration_days: int = Field(default=0, ge=0, le=MAX_EXPIRY_DAYS)  # 0 = never expires
    force_new: bool = False
    listen_set: list[str] | None = Field(default=None, max_length=MAX_LIST)
    reader_identity: str | None = Field(default=None, max_length=MAX_ADDR)

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
    query: str = Field(max_length=MAX_VALUE)
    scope: str = "user"
    user_id: str = Field(default="default", max_length=MAX_ADDR)
    project: str | None = Field(default=None, max_length=MAX_ADDR)
    limit: int = Field(default=5, ge=1, le=MAX_SEARCH_LIMIT)
    listen_set: list[str] | None = Field(default=None, max_length=MAX_LIST)
    reader_identity: str | None = Field(default=None, max_length=MAX_ADDR)

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
    # SEC-6 — extra="forbid" is the single most important line in this model.
    #
    # On 2026-07-23 a caller sent {"key_prefix": "inbox/", "confirm": false}
    # believing `confirm:false` meant "dry run". No such field existed. Pydantic
    # silently ignored it, the delete ran for real, and 1733 rows — every inbox
    # message on the fleet — were destroyed with no backup to restore from.
    #
    # An endpoint that ACCEPTS an unknown safety flag is worse than one with no
    # safety flag at all: it returns success and actively confirms the caller's
    # false belief. A 422 would have cost nothing and prevented all of it.
    model_config = {"extra": "forbid"}

    namespace: str
    key_prefix: str
    scope: str | None = None
    user_id: str | None = None
    older_than_days: int | None = None
    # Real dry-run. Defaults to TRUE: the safe mode is what you get when you
    # don't think about it, and destroying data requires saying so explicitly.
    # This inverts the old default, and that is deliberate — see the router.
    dry_run: bool = True
    # Required when the predicate is broad enough to match a whole class of
    # keys. Names what you intend to destroy, so a wide blast radius has to be
    # stated rather than stumbled into.
    i_understand_this_deletes: str | None = None

    @field_validator("key_prefix", mode="before")
    @classmethod
    def key_prefix_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("key_prefix must not be empty")
        return v


class BulkDeleteResponse(BaseModel):
    status: str
    deleted_count: int
    # What a dry run reports. `matched_count` is what WOULD be deleted; on a
    # real delete it equals deleted_count. Distinguishing the two is the point:
    # the incident happened because a match count was read as a preview.
    matched_count: int | None = None
    dry_run: bool = False
    sample_keys: list[str] = []
    guidance: str | None = None


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
    body: str = Field(max_length=MAX_VALUE)
    subject: str = Field(default="", max_length=MAX_SUBJECT)
    from_: str | None = Field(default=None, max_length=MAX_ADDR)  # sender identity — MCP bridge stamps this
    thread_id: str | None = Field(default=None, max_length=MAX_ADDR)
    supersedes: str | None = Field(default=None, max_length=MAX_ADDR)  # id of a prior message this one replaces
    intent: str | None = None  # fyi | action | proceed | escalate | authority-directive
    # ADDR-1: the sender's REAL listen_set, for the addressing guidance echoed
    # back. The server cannot reconstruct it from ``from_`` — once a session
    # holds a seat, the identity string carries neither the project group
    # address nor channel subscriptions. Optional: older bridges omit it and
    # get the (clearly-labelled) approximation instead.
    listen_set: list[str] | None = Field(default=None, max_length=MAX_LIST)

    @field_validator("to")
    @classmethod
    def to_bounded(cls, v):
        if isinstance(v, list) and len(v) > MAX_LIST:
            raise ValueError(f"too many recipients (max {MAX_LIST})")
        return v

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
    # HUD-1 — membership of a private multi-party thread, fixed at send time.
    # Empty on ordinary 1:1 mail. Non-empty means a reply should fan out to
    # everyone here (minus the replier), which is what makes a hand-picked
    # group behave as a conversation instead of N parallel DMs.
    participants: list[str] = []
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
    listen_set: list[str] = Field(max_length=MAX_LIST)
    reader_identity: str | None = Field(default=None, max_length=MAX_ADDR)
    unread_only: bool = True
    include_resolved: bool = False  # default view hides resolved/superseded
    limit: int = Field(default=20, ge=1, le=MAX_INBOX_LIMIT)
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
    identity: str = Field(max_length=MAX_ADDR)   # inbox identity, e.g. "proj-grok"
    project: str = Field(max_length=MAX_ADDR)    # bare project name (roster grouping key)
    state: str               # running | awaiting-input | done
    provider: str | None = Field(default=None, max_length=MAX_ADDR)  # claude | grok | codex | ...
    overlays: list[str] = Field(default=[], max_length=MAX_LIST)     # e.g. ["proj/builders"]
    channels: list[str] = Field(default=[], max_length=MAX_LIST)     # e.g. ["#courseware"]
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


# --- Seat registry (SEAT-3) ----------------------------------------------
#
# Sessions CLAIM an address rather than computing one, so N sessions in a
# project get N distinct addresses with no human step. See
# docs/design/session-registry.md.


class SeatClaimRequest(BaseModel):
    # Stable per-session key. CONTINUITY, not identity: re-claiming with the
    # same key returns the same seat, so a bridge restart never moves a
    # session's address. Launcher-injected (ENGRAM_SESSION_KEY) or derived by
    # the bridge from its harness parent process.
    session_key: str = Field(max_length=MAX_ADDR)
    project: str = Field(max_length=MAX_ADDR)
    provider: str = Field(default="claude", max_length=MAX_ADDR)
    # Per-PROCESS nonce. With session_key it forms IDENTITY: a known key
    # arriving with a different nonce while the holder is still live means two
    # processes share one key, not that one restarted.
    session_nonce: str | None = None
    host: str | None = Field(default=None, max_length=MAX_ADDR)
    # What the caller would LIKE (e.g. a launcher's "<project>-<provider>").
    # A preference, never an assignment — the server grants it when free.
    preferred_seat: str | None = Field(default=None, max_length=MAX_ADDR)

    @field_validator("session_key", "project", mode="before")
    @classmethod
    def claim_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v.strip().lower()


class SeatClaimResponse(BaseModel):
    status: str
    seat: str
    is_new: bool = False
    # Set when this seat was taken over from an abandoned session — surfaced so
    # a reclaim is visible in logs rather than silent.
    reclaimed_from: str | None = None
    # Set when the caller's session_key is provably not unique. Loud on
    # purpose: the failure it describes is otherwise silent.
    warning: str | None = None
    guidance: str | None = None


class SeatReleaseRequest(BaseModel):
    session_key: str = Field(max_length=MAX_ADDR)
    project: str = Field(max_length=MAX_ADDR)

    @field_validator("session_key", "project", mode="before")
    @classmethod
    def release_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v.strip().lower()


class SeatReleaseResponse(BaseModel):
    status: str
    released: str | None = None


class SeatAliasRequest(BaseModel):
    session_key: str = Field(max_length=MAX_ADDR)
    project: str = Field(max_length=MAX_ADDR)
    # The ROLE, e.g. "orchestrator". Bound as "<project>-<alias>" and ADDED to
    # the session's listen_set — never a rename of the seat.
    alias: str = Field(max_length=MAX_ADDR)

    @field_validator("session_key", "project", "alias", mode="before")
    @classmethod
    def alias_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v.strip().lower()


class SeatAliasResponse(BaseModel):
    status: str
    seat: str
    aliases: list[str] = []


class SeatEntry(BaseModel):
    seat: str
    project: str | None = None
    provider: str | None = None
    host: str | None = None
    aliases: list[str] = []
    session_key: str | None = None
    age_seconds: float
    is_live: bool
    reclaimable: bool


class SeatListRequest(BaseModel):
    project: str | None = None
    # Direct lookup for a LAUNCHER: "what seat does the session I spawned
    # actually hold?" A launcher never calls /session/claim — the bridge
    # inside the session does — so the granted seat has to be readable by the
    # one join a launcher owns: the session key it generated.
    session_key: str | None = Field(default=None, max_length=MAX_ADDR)


class SeatListResponse(BaseModel):
    status: str
    seats: list[SeatEntry] = []
