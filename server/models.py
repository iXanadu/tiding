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
MAX_KEYS_LIMIT = 2000     # MEM-2 enumeration cap; `total` makes truncation legible
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
    # SEC-7 (locked "warn", 2026-07-27, unanimous): unknown fields are
    # ACCEPTED and REPORTED, not rejected and not silently dropped. A
    # misspelled option (`if_matched` for `if_match`) used to vanish in
    # pydantic's default extra=ignore — the write proceeded unguarded and
    # only the absence of `if_match_applied: true` hinted why. Rejecting
    # (extra=forbid) would name the typo instantly but break any shipped
    # client sending a stray field — engram is public, and a fielded app
    # binary cannot be hotfixed. So: extra="allow" captures them, and the
    # handler surfaces `warning: "unknown fields ignored: …"` in the
    # response. Non-breaking; the typo is visible at the first read.
    model_config = {"extra": "allow"}

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
    # MEM-4 optimistic concurrency. Set to the `version` you read and the
    # write proceeds only if the stored value is unchanged — the guard for a
    # read-modify-write, e.g. several agents each rewriting their own section
    # of one shared handoff. `""` asserts the row does not exist yet. Omit for
    # unconditional write (the default, unchanged behavior).
    if_match: str | None = Field(default=None, max_length=64)

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
    # Cap each result's `value` at this many lines. Search is for FINDING;
    # reading in full is what memory_get is for. A startup sweep of four
    # searches routinely returned the same 1,200-word handoff three times over,
    # spending a large share of a context window re-reading identical text.
    # Opt-in (None = unchanged, full values) because this is a public API.
    # Truncation is always ANNOUNCED in the value — a partial read must never
    # look like a complete one.
    snippet_lines: int | None = Field(default=None, ge=1, le=500)
    # MEM-3: superseded rows are hidden by default — a corrected note that
    # still ranks is still giving instructions. True returns them, marked
    # (item.status == "superseded"), for audit/history reads.
    include_superseded: bool = False

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
    # MEM-4: content hash of `value`. Pass it back as `if_match` on a later
    # write to make that write conditional — the read-modify-write guard for
    # callers that edit part of a shared document.
    version: str | None = None
    # MEM-3 lifecycle. None = live (every pre-lifecycle row). "superseded"
    # appears only on include_superseded reads — default search excludes them.
    status: str | None = None


class InboxBanner(BaseModel):
    # The TRUE number of unread messages, not the length of `preview`. These
    # were the same field until 2026-07-27, which capped the reported count at
    # the preview window and told a session with 130 unread that it had 6.
    unread_count: int
    preview: list[str]  # ≤5 one-line previews, newest first
    # How many of `unread_count` the preview actually lists, so a renderer can
    # say "6 of 130" rather than presenting a page size as a total.
    shown: int | None = None


class MemorySetResponse(BaseModel):
    status: str
    key: str
    # The CANONICAL namespace the write landed in (request namespaces are
    # alias-canonicalized at the boundary) — lets clients display the truth
    # instead of echoing their possibly-legacy configured name.
    namespace: str | None = None
    # MEM-1: False means this write OVERWROTE an existing value. Memory
    # identity carries no session dimension (deliberately — the work outlives
    # the session), so two sessions writing one key destroy each other's
    # value. Until this field existed both got an identical "stored" response
    # and the loser could not tell. Clients should surface an overwrite that
    # the caller may not have intended.
    created: bool | None = None
    # MEM-4: content hash of what is now stored. Pass it as `if_match` on a
    # follow-up write to make that write conditional.
    version: str | None = None
    # MEM-4 SAFETY SIGNAL. True means the conditional guard actually ran for
    # this request; False means the write was unconditional.
    #
    # This exists because the failure it prevents is silent and severe: a
    # client sending `if_match` to a server that PREDATES MEM-4 has the field
    # dropped by pydantic and the write proceeds UNGUARDED, while the client
    # believes it was protected — the exact shape of the `confirm: false` flag
    # that did not exist and cost 1733 inbox rows on 2026-07-23. A server
    # cannot be fixed retroactively, so the signal has to be something a NEW
    # server emits and an old one cannot: absence of `true` here means the
    # guard did NOT run. Never read a missing field as success.
    if_match_applied: bool | None = None
    # MEM-3 honesty: non-empty when this project-scope write has same-key
    # siblings under OTHER writers — the caller just forked (or re-forked) a
    # duplicate key and should know the other rows exist.
    partition_warnings: list[str] = []
    # SEC-7: set when the request carried fields this server does not know —
    # almost always a misspelled option (`if_matched`). The write succeeded
    # WITHOUT those fields; this line is what turns "why do my merges never
    # happen" into a one-glance answer.
    warning: str | None = None
    inbox_banner: InboxBanner | None = None


class MemoryGetResponse(BaseModel):
    status: str
    memory: MemoryItem | None = None
    # MEM-3 honesty: on a not_found where the SAME key exists under other
    # writers in this project, name them — a partition miss must not be
    # indistinguishable from the key not existing (measured stalling a
    # cleanup for a full session, 2026-08-10).
    partition_warnings: list[str] = []


class MemorySearchResponse(BaseModel):
    status: str
    results: list[MemoryItem]
    inbox_banner: InboxBanner | None = None


class MemoryKeysRequest(_NamespacedRequest):
    """MEM-2: deterministic key enumeration — the verb between get and search."""
    namespace: str | None = None
    namespaces: list[str] | None = None
    prefix: str = Field(default="", max_length=MAX_KEY)
    scope: str = "user"
    user_id: str = Field(default="default", max_length=MAX_ADDR)
    project: str | None = Field(default=None, max_length=MAX_ADDR)
    limit: int = Field(default=500, ge=1, le=MAX_KEYS_LIMIT)

    def explicit_namespaces(self) -> list[str] | None:
        """Namespaces the client explicitly asked for, or None to let the
        server resolve from the principal — same contract as search."""
        if self.namespaces:
            return self.namespaces
        if self.namespace:
            return [self.namespace]
        return None


class KeyEntry(BaseModel):
    namespace: str
    key: str
    scope: str
    user_id: str | None = None
    project: str | None = None
    tags: str = ""
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    # An index entry, not the content: enumeration answers "what exists",
    # memory_get answers "what does it say". The length is served so a reader
    # can tell a one-line stub from a real note without fetching either.
    value_chars: int = 0
    # Lifecycle, unfiltered: superseded rows are listed and MARKED, because a
    # census that hides corrected rows cannot prove a write ever happened.
    status: str | None = None


class MemoryKeysResponse(BaseModel):
    status: str
    keys: list[KeyEntry]
    # Full match count. When len(keys) < total the listing is truncated at
    # the requested limit — served so truncation is legible, never silent.
    total: int = 0


class MemoryForgetResponse(BaseModel):
    status: str
    key: str
    # MEM-3 honesty — same contract as MemoryGetResponse.partition_warnings.
    partition_warnings: list[str] = []


class MemorySupersedeRequest(_NamespacedRequest):
    """Mark another writer's row as superseded (MEM-3; shared scope: MEM-7).

    scope is 'project' (default) or 'shared': user/machine scopes are personal
    and no peer gets a lifecycle verb over them. The row is kept verbatim —
    this changes what default search RETURNS, never what history recorded.
    """
    namespace: str
    key: str
    scope: str = Field(default="project", pattern="^(project|shared)$")
    project: str | None = Field(default=None, max_length=MAX_ADDR)
    # The WRITER whose row is being retired — from search results' user_id.
    target_user_id: str = Field(max_length=MAX_ADDR)
    # Required: becomes the audit trail a future reader sees.
    reason: str = Field(min_length=3, max_length=2000)
    # The key that replaces it, when one exists — renders as a redirect.
    replacement_key: str | None = Field(default=None, max_length=MAX_KEY)


class MemorySupersedeResponse(BaseModel):
    status: str
    key: str
    target_user_id: str
    # Canonical namespace the row was found in (may differ from the request's).
    namespace: str | None = None
    guidance: str | None = None


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
    # LANE-5: the sender's LANE (`<project>-<provider>`, the immortal mailbox
    # it listens on). Stamped by the sending bridge — like listen_set, the
    # server cannot derive it from `from_` (a seated identity string carries
    # neither project nor provider). Recipients' replies target this instead
    # of the mortal seat, so a reply composed after the sender dies still
    # reaches the lane's next occupant. Optional: older bridges omit it and
    # replies to their mail keep the legacy seat routing.
    from_lane: str | None = Field(default=None, max_length=MAX_ADDR)

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
    # Client-supplied provenance — recorded, never proof (contrast
    # `from_principal`, which is derived from the token). `machine` is which box
    # sent it; `model` is what produced it, read by the bridge from the harness's
    # own record. `model_source` (transcript | declared | harness-config |
    # unknown) says how well that model is known, and is present even when
    # `model` is None so a blank stays legible as a blank rather than ambiguous
    # between "records nothing" and "predates the stamp".
    machine: str | None = None
    model: str | None = None
    model_source: str | None = None
    # LANE-5: the sender's immortal lane, when its bridge stamped one — the
    # address a reply should target so it survives the sender's death.
    from_lane: str | None = None
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
    # Recipients that have a presence row and look dead/stale, when the intent
    # was one that expects to WAKE somebody. Delivery still succeeded — the
    # message is stored and will be read whenever that address next runs — so
    # this is a warning, never an error. Empty/absent when every recipient
    # looks live or has no presence row at all (absent is not dead).
    recipient_warnings: list[str] | None = None


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


# --- Per-sender unread summary (session-card badge) -----------------------

class InboxUnreadSummaryRequest(BaseModel):
    """Who has DIRECT mail waiting for this reader, and how much.

    ``reader_identity`` is required (unlike InboxListRequest, where it is
    optional): the whole answer is "unread BY THIS READER", so without it
    there is no question to answer — and defaulting it would silently return
    somebody else's count.
    """
    listen_set: list[str] = Field(max_length=MAX_LIST)
    reader_identity: str = Field(max_length=MAX_ADDR)


class InboxUnreadSender(BaseModel):
    from_: str = Field(alias="from")
    unread: int
    latest: datetime | None = None

    model_config = {"populate_by_name": True}


class InboxUnreadSummaryResponse(BaseModel):
    status: str
    senders: list[InboxUnreadSender]
    total: int
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
    # MSG-5: this beat came from the inbox WATCHER, not the session itself.
    # Routed to a narrow write that records only "an ear is alive here" — it
    # carries no state and joins no nonce map, because the watcher shares its
    # session's identity and would otherwise look like a second live session.
    watcher: bool = False
    # The watcher OBSERVED this session's process exit. Not a self-report: a
    # dying process is a poor reporter (it may never be scheduled, and SIGKILL
    # gives it nothing to say), so the one process that outlives the session
    # reports the transition instead. Routed to a narrow write like `watcher`.
    #
    # ⚠️ ASYMMETRIC BY CONSTRUCTION: receiving this is evidence of death;
    # NOT receiving it is evidence of nothing whatsoever. Never infer from
    # absence — see test_a_missing_goodbye_changes_nothing.
    farewell: bool = False

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
    # ⚠️ BACK-COMPAT SHIM, restored 2026-08-01 hours after removal. REMOVE ONLY
    # when no pre-2026-08-01 bridge is still running anywhere on the fleet.
    #
    # Removing this field broke every LIVE session: the shipped bridge renders
    # the roster with `f"{e['state']:<15}"` — a direct subscript, not `.get()` —
    # so `memory_roster` raised KeyError for any session started before the
    # deploy. Bridge changes land at a session's NEXT start, so "we updated the
    # bridge" fixes nobody already running.
    #
    # The migration was checked with the peer consumer that had asked for the
    # removal, and their clearance was mistaken for fleet clearance. THE BRIDGE
    # IS ALSO A CONSUMER, and every running session holds an old copy of it.
    # A wire contract has as many consumers as there are deployed readers, not
    # as many as there are maintainers who answered.
    #
    # Value is the session's own recorded claim, and where there is none it is
    # "unknown" — NOT the invented "running" default this field originally
    # carried. Back-compatible without restoring the lie.
    state: str = "unknown"
    # `state` was removed here on 2026-08-01 for good reasons that still hold. It was
    # kept as "the session's own last claim, never corrected" — defensible in
    # principle, empty in fact: one distinct value across all 38 presence rows
    # ever recorded, `running`, plus a server-side default that INVENTED that
    # claim for rows whose metadata said nothing.
    #
    # A constant printed beside a name reads as a status. It carried zero bits
    # and cost a consumer an evening of chasing sessions the word said were
    # alive. The roster reports facts — this address exists, something last
    # spoke at T, a watcher beat at T2 — and liveness verdicts belong to
    # whatever spawns and kills, which observes a termination instead of
    # inferring it. Still RECORDED in presence metadata as provenance; simply
    # no longer asserted on the wire.
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
    # When a watcher OBSERVED this session's process exit. None means no such
    # observation exists — never that the session is alive. Voided by any
    # later evidence of life, so a wrong one self-heals.
    farewell_at: datetime | None = None
    # MSG-5: is anyone actually LISTENING at this address? True = a watcher
    # beat recently; False = one used to beat and has gone quiet; None = no
    # watcher has ever beaten here, so there is no basis. None is never
    # coerced to False — absent is not dead. The state worth acting on is
    # is_stale=False with watcher_alive=False: running, addressable, and deaf.
    watcher_alive: bool | None = None
    watcher_last_seen: datetime | None = None


class RosterRequest(BaseModel):
    project: str | None = None   # None = whole-box roster
    channel: str | None = None   # filter to members of a #channel
    include_done: bool = False   # done sessions hidden by default
    # SEAT-4 retention horizon: rows silent >48h are hidden by default. They
    # are never deleted — set this to see them. Sixteen corpses burying five
    # live sessions made the owner's huddle picker unusable, which is what
    # this defends against; the roster answers "who can I reach now".
    include_expired: bool = False


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


class InboxResolveThreadRequest(BaseModel):
    """Drain a whole thread — a closed room, a finished exchange.

    ``listen_set`` scopes it to the caller's own copies. A fan-out lands one
    row per recipient, and one participant declaring the room finished must
    not drain it out from under the others.
    """
    thread_id: str
    listen_set: list[str]
    reader_identity: str | None = None

    @field_validator("thread_id", mode="before")
    @classmethod
    def thread_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("'thread_id' must be a non-empty string")
        return v.strip()


class InboxResolveThreadResponse(BaseModel):
    status: str
    thread_id: str
    resolved: int
    guidance: str | None = None


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
    # ID-2: preferred_seat was chosen DELIBERATELY mid-session
    # (memory_take_seat), so continuity must MOVE the registration to it
    # rather than answer with the seat already held. Without this the tool
    # and the registry fought — the runtime seat was silently reverted
    # within one heartbeat.
    runtime_seat: bool = False

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
    # Set when the caller's session_key is provably not unique, or when a
    # runtime re-seat was REFUSED (name held by another session). Loud on
    # purpose: the failure it describes is otherwise silent.
    warning: str | None = None
    # ID-2: set when this claim MOVED the registration to a runtime-declared
    # seat — the old name, so launchers (AgentBeast) can reconcile their
    # registry against the seat the session is actually on.
    renamed_from: str | None = None
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


class SeatEntry(BaseModel):
    seat: str
    project: str | None = None
    provider: str | None = None
    host: str | None = None
    session_key: str | None = None
    # SEAT-16: True when session_key carries the bridge's generated-key marker
    # (`auto-` prefix) — the key names a harness PROCESS and will not survive
    # a respawn, so continuity claims against it are process-lifetime only.
    # A fact about the key's minting, not a verdict (additive; WIRE-1 rule:
    # fields are added with defaults, never removed on one consumer's say-so).
    session_key_generated: bool = False
    # SEATS-1: the watcher's beat for this seat, in the roster's three-valued
    # vocabulary — True: beat within the freshness window, mail will wake it;
    # False: a watcher has beaten here before and went quiet; None: no watcher
    # has EVER beaten here (no basis — an unarmed session or a pre-beat
    # bridge). None must never be read as False: absent is not dead.
    watcher_alive: bool | None = None
    watcher_last_seen: str | None = None
    # Both `is_live` and `reclaimable` were removed 2026-08-01. Each was a
    # threshold applied to `age_seconds` and nothing else, so exporting them
    # sent the same bit twice — once as a fact, once as a verdict. Consumers
    # judge from the number; the allocation backstop stays internal.
    age_seconds: float


class DeathCertRequest(BaseModel):
    """LANE-4 (docs/design/immortal-addresses.md §6): a SPAWNER certifies an
    occupant's death. Testimony, not inference — the store never decides a
    session is dead; it records the word of the party that performed or
    observed the kill, and the certificate WINS over a still-beating presence
    row (a heartbeat can outlive a kill; it can never observe one).

    Facts only, with power scoped narrowly: the cert feeds the lane read-
    cursor (succession inheritance) and the record. It does NOT free the
    seat or accelerate reclamation — that question is SEAT-13, the owner's,
    deliberately untouched.
    """
    # Primary idempotency key. "" allowed — grok's start path cannot always
    # inject one (SEAT-6); then (seat, died_at) is the fallback key.
    session_key: str = Field(default="", max_length=MAX_ADDR)
    # The GRANTED occupant seat, or "" when the spawner never learned it.
    # Never the lane: a spawner-side seat_for() fallback would certify that
    # the LANE died (the trap named in review). Enforced only once
    # reservation is on — pre-reservation, an honest first occupant's granted
    # seat legitimately IS the lane string (PM amendment, 2026-08-14).
    seat: str = Field(default="", max_length=MAX_ADDR)
    lane: str = Field(default="", max_length=MAX_ADDR)
    project: str = Field(max_length=MAX_ADDR)
    provider: str = Field(max_length=MAX_ADDR)
    # Read-state keys on host-qualified reader identities (<seat>@<host>), so
    # harvesting the dead occupant's acks needs the host. "" degrades to a
    # seat@% match (single-box safe).
    host: str = Field(default="", max_length=MAX_ADDR)
    died_at: datetime
    # Canonical: stop | reconcile | spawn-failed. Other short strings are
    # stored verbatim — the vocabulary belongs to the certifier.
    cause: str = Field(default="", max_length=64)
    graceful: bool | None = None

    @field_validator("session_key", "seat", "lane", "project", "provider",
                     "host", mode="before")
    @classmethod
    def death_normalize(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("project", "provider", mode="before")
    @classmethod
    def death_not_empty(cls, v):
        if not isinstance(v, str) or not v.strip():
            raise ValueError("must be a non-empty string")
        return v


class DeathCertResponse(BaseModel):
    status: str
    created: bool
    cursor_updated: bool = False
    cursor_size: int = 0


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
