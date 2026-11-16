"""One-process session store with atomic CAS and bounded request replay."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from src.ktru import contracts as C
from src.ktru.service.errors import _err

Snapshot = tuple[str, list[dict], list[dict]]


@dataclass
class Session:
    id: str
    snapshot_id: str
    revision: int = 0
    text: str = ""
    answers: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    history: list[Snapshot] = field(default_factory=list)
    llm: dict | None = None
    corrections: dict[str, list[C.Correction]] = field(default_factory=dict)  # Automatic text fixes by accepted text
    pending: dict[str, dict] = field(default_factory=dict)
    state: C.CardState | None = None
    requests: OrderedDict[str, tuple[str, C.CardState]] = field(default_factory=OrderedDict)
    request_sizes: dict[str, int] = field(default_factory=dict)
    cached_state_bytes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    analyzing: set[int] = field(default_factory=set)


class InMemorySessionStore:
    """Keep sessions in one API process. Restart and LRU eviction expire sessions."""

    def __init__(self, *, max_sessions: int | Callable[[], int] = 500,
                 max_cached_requests: int | Callable[[], int] = 20,
                 max_cached_state_bytes: int | Callable[[], int] = 128 * 1024):
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._created: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.RLock()
        self.max_sessions = max_sessions
        self.max_cached_requests = max_cached_requests
        self.max_cached_state_bytes = max_cached_state_bytes

    @staticmethod
    def _limit(value: int | Callable[[], int]) -> int:
        return value() if callable(value) else value

    def create(self, snapshot_id: str) -> Session:
        session = Session(id=uuid.uuid4().hex, snapshot_id=snapshot_id)
        with self._lock:
            self._sessions[session.id] = session
            while len(self._sessions) > self._limit(self.max_sessions):
                self._sessions.popitem(last=False)
        return session

    def read(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise _err(404, "session_expired", "Сессия не найдена или устарела. Начните заново.")
            self._sessions.move_to_end(session_id)
            return session

    def get_or_create(self, request_id: str, snapshot_id: str) -> Session:
        with self._lock:
            existing = self._created.get(request_id)
            if existing is not None:
                return self.read(existing)
            session = self.create(snapshot_id)
            self._created[request_id] = session.id
            while len(self._created) > self._limit(self.max_sessions):
                self._created.popitem(last=False)
            return session

    @staticmethod
    def _digest(payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def replay(self, session: Session, request_id: str, payload: dict, expected_revision: int) -> C.CardState | None:
        with session.lock:
            return self._replay(session, request_id, self._digest(payload), expected_revision)

    @staticmethod
    def _replay(session: Session, request_id: str, digest: str, expected_revision: int) -> C.CardState | None:
        cached = session.requests.get(request_id)
        if cached:
            if cached[0] != digest:
                raise _err(409, "request_conflict", "Повтор запроса с другим содержимым.", request_id=request_id)
            return cached[1].model_copy(deep=True)
        if expected_revision != session.revision:
            raise _err(409, "stale_revision", "Данные изменились: показано актуальное состояние.",
                       retryable=True, revision=session.revision, request_id=request_id)
        return None

    def cas(self, session: Session, request_id: str, payload: dict, expected_revision: int,
            transition: Callable[[Session], C.CardState]) -> C.CardState:
        digest = self._digest(payload)
        with session.lock:
            cached = self._replay(session, request_id, digest, expected_revision)
            if cached is not None:
                return cached
            candidate = replace(session, answers=copy.deepcopy(session.answers), edits=copy.deepcopy(session.edits),
                history=copy.deepcopy(session.history), llm=copy.deepcopy(session.llm), pending=copy.deepcopy(session.pending),
                state=session.state.model_copy(deep=True) if session.state else None,
                requests=copy.deepcopy(session.requests), request_sizes=dict(session.request_sizes),
                analyzing=set(session.analyzing))
            state = transition(candidate)
            if (state.snapshot_id, state.session_id, state.revision) != (session.snapshot_id, session.id, session.revision + 1):
                raise ValueError("Transition changed the pinned snapshot, session identity or next revision")
            size = len(state.model_dump_json().encode())
            published = state.model_copy(deep=True)
            requests = OrderedDict(session.requests)
            requests[request_id] = (digest, published)
            sizes = {**session.request_sizes, request_id: size}
            cached_bytes = session.cached_state_bytes + size
            while requests and (len(requests) > self._limit(self.max_cached_requests)
                                or cached_bytes > self._limit(self.max_cached_state_bytes)):
                removed, _ = requests.popitem(last=False)
                cached_bytes -= sizes[removed]
                sizes = {key: value for key, value in sizes.items() if key != removed}
            session.text, session.answers, session.edits = candidate.text, candidate.answers, candidate.edits
            session.history, session.llm, session.pending = candidate.history, candidate.llm, candidate.pending
            session.revision, session.state = published.revision, published
            session.requests, session.request_sizes, session.cached_state_bytes = requests, sizes, cached_bytes
            return published.model_copy(deep=True)
