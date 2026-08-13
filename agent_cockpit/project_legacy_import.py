"""PROJ-003 legacy import — read-only evidence orchestration into Project Registry.

Reads legacy Agent Mail, ``mail-projects.json``, Herdr persisted sessions and
Coordination runs WITHOUT mutation, normalizes deterministic provenance, groups
by exact canonical local path, and calls the accepted Store primitive
``ProjectRegistryStore.import_legacy_project`` exactly once per candidate.

Hard boundaries:
- Never opens the Registry via raw SQL; all registry writes go through the Store.
- Never writes/mutates any legacy authority (Agent Mail DB, mail JSON, Herdr,
  Coordination DB, Git, runtime). All legacy reads are ``mode=ro`` / plain reads.
- Never creates Workspace/Run/Agent/pane/Git state — the Store primitive owns the
  atomic Project+RepoLocation+provenance insert and nothing else.
- Never calls ``Path.resolve()`` across an unbounded gap; candidate paths are
  lexically validated and must fall under the configured local boundary.

Idempotency/replay is owned by the Store (exact replay returns original IDs,
``replayed=True``, no row/timestamp change). The importer only guarantees
deterministic candidate enumeration and one Store call per candidate; a crash may
leave earlier candidates committed but never a half candidate (rerun converges).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from . import project_registry_domain as domain
from . import project_registry_store


# ── stable error codes (ASCII, no path/payload/SQL) ────────────────────────
SOURCE_UNAVAILABLE = "source_unavailable"
SOURCE_CORRUPT = "source_corrupt"
SOURCE_NOT_FOUND = "source_not_found"
UNVERIFIED_PATH = "unverified_path"
EMPTY_SOURCES = "empty_sources"
EVIDENCE_CONFLICT = "evidence_conflict"
AUTHORITY_DISAGREEMENT = "authority_disagreement"


AGENT_MAIL_PROJECTS_FINGERPRINT = (
    (0, "id", "INTEGER", 1, None, 1),
    (1, "slug", "VARCHAR(255)", 1, None, 0),
    (2, "human_key", "VARCHAR(255)", 1, None, 0),
    (3, "created_at", "DATETIME", 1, None, 0),
    (4, "archived_at", "DATETIME", 0, None, 0),
)

COORDINATION_RUNS_FINGERPRINT = (
    (0, "run_id", "TEXT", 0, None, 1),
    (1, "project_key", "TEXT", 1, None, 0),
    (2, "session", "TEXT", 1, None, 0),
    (3, "session_dir", "TEXT", 1, None, 0),
    (4, "revision", "INTEGER", 1, None, 0),
    (5, "state", "TEXT", 1, None, 0),
    (6, "config_hash", "TEXT", 1, None, 0),
    (7, "started_ts", "REAL", 1, None, 0),
    (8, "closed_ts", "REAL", 0, None, 0),
)


# ── canonicalization (frozen; vectors in tests assert exact sha256) ────────
def canonical_json(value: Any) -> str:
    return domain.canonical_json(value)


def _sha256_uri(value: Any) -> str:
    return "sha256:" + __import__("hashlib").sha256(canonical_json(value).encode("ascii")).hexdigest()


# ── importer-side value types ──────────────────────────────────────────────
@dataclass(frozen=True)
class LegacySourceEvidence:
    source_kind: str
    source_key: str
    source_digest: str


@dataclass(frozen=True)
class CandidateProject:
    slug: str
    display_name: str
    goal: str | None
    node_id: str
    canonical_path: str
    vcs_kind: str
    availability: str
    sources: tuple[LegacySourceEvidence, ...]


@dataclass(frozen=True)
class SourceStatus:
    kind: str
    state: str  # "ok" | "unavailable" | "error"
    detail_code: str | None = None
    count: int = 0


@dataclass(frozen=True)
class CandidateOutcome:
    canonical_path: str
    state: str  # "committed" | "replayed" | "conflict" | "skipped"
    error_code: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class ImportReport:
    candidates: tuple[CandidateOutcome, ...]
    sources: tuple[SourceStatus, ...]
    complete: bool


@dataclass(frozen=True)
class LegacyRoots:
    agent_mail_db: Path | None = None
    mail_projects_json: Path | None = None
    herdr_sessions_dir: Path | None = None
    coordination_db: Path | None = None
    local_boundary: Path | None = None


@dataclass(frozen=True)
class SourceReadResult:
    kind: str
    state: str  # "ok" | "unavailable" | "error"
    records: tuple[dict[str, Any], ...] = ()
    detail_code: str | None = None


# ── path safety (lexical, no Path.resolve across unbounded gap) ────────────
def _validate_candidate_path(path: str, boundary: Path | None) -> str:
    """Strict lexical canonical-path validation + optional boundary containment."""
    try:
        cleaned = domain.canonical_path(path)
    except ValueError as exc:
        raise _ImportError(UNVERIFIED_PATH) from exc
    if "\x00" in cleaned:
        raise _ImportError(UNVERIFIED_PATH)
    if boundary is not None:
        b = str(boundary)
        if not (cleaned == b or cleaned.startswith(b.rstrip("/") + "/")):
            raise _ImportError(UNVERIFIED_PATH)
    return cleaned


class _ImportError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# ── source readers (read-only) ─────────────────────────────────────────────
class LegacySourceReader(Protocol):
    kind: str

    def read(self, roots: LegacyRoots) -> SourceReadResult: ...


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)


class AgentMailProjectReader:
    """Read unarchived Agent Mail projects (mode=ro). Authority: mail/identities."""

    kind = "agent_mail_project"

    def read(self, roots: LegacyRoots) -> SourceReadResult:
        db = roots.agent_mail_db
        if db is None:
            return SourceReadResult(self.kind, "unavailable", detail_code=SOURCE_NOT_FOUND)
        if not db.is_file():
            return SourceReadResult(self.kind, "unavailable", detail_code=SOURCE_NOT_FOUND)
        try:
            con = _connect_ro(db)
        except sqlite3.Error:
            return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
        try:
            fingerprint = tuple(
                tuple(r) for r in con.execute("PRAGMA table_info(projects)").fetchall()
            )
            if fingerprint != AGENT_MAIL_PROJECTS_FINGERPRINT:
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            rows = con.execute(
                "SELECT id, slug, human_key FROM projects WHERE archived_at IS NULL"
            ).fetchall()
        except sqlite3.Error:
            return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
        finally:
            con.close()
        records: list[dict[str, Any]] = []
        for row in rows:
            if type(row[0]) is not int:
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            pid = row[0]
            if row[1] is not None and not isinstance(row[1], str):
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            if row[2] is not None and not isinstance(row[2], str):
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            slug = row[1] or ""
            human_key = row[2] or ""
            if not human_key:
                continue
            records.append({"project_id": pid, "slug": slug, "human_key": human_key})
        return SourceReadResult(self.kind, "ok", records=tuple(records))


class MailProjectsJsonReader:
    """Strict read of mail-projects.json. Never uses mail_projects._load (silent)."""

    kind = "mail_projects_session"

    def read(self, roots: LegacyRoots) -> SourceReadResult:
        path = roots.mail_projects_json
        if path is None or not path.is_file():
            return SourceReadResult(self.kind, "unavailable", detail_code=SOURCE_NOT_FOUND)
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except (OSError, UnicodeError, ValueError):
            return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
        if not isinstance(data, dict) or set(data) != {"version", "sessions"}:
            return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
        if type(data["version"]) is not int or data["version"] != 1:
            return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
        sessions = data["sessions"]
        if not isinstance(sessions, dict):
            return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
        if not sessions:
            return SourceReadResult(self.kind, "ok", records=())
        records: list[dict[str, Any]] = []
        for name, entry in sessions.items():
            if not isinstance(name, str) or not name or not isinstance(entry, dict):
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            if set(entry) != {"session_dir", "project"}:
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            sd = entry.get("session_dir")
            proj = entry.get("project")
            if not isinstance(sd, str) or not isinstance(proj, str) or not sd or not proj:
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            records.append(
                {"session": name, "session_dir": sd, "project": proj, "version": 1}
            )
        return SourceReadResult(self.kind, "ok", records=tuple(records))


class HerdrSessionReader:
    """Read Herdr persisted session descriptors (read-only). Observational only."""

    kind = "herdr_session"

    def read(self, roots: LegacyRoots) -> SourceReadResult:
        directory = roots.herdr_sessions_dir
        if directory is None or not directory.is_dir():
            return SourceReadResult(self.kind, "unavailable", detail_code=SOURCE_NOT_FOUND)
        records: list[dict[str, Any]] = []
        try:
            files = sorted(p for p in directory.glob("*.json") if p.is_file())
        except OSError:
            return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
            except (OSError, UnicodeError, ValueError):
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            if not isinstance(data, dict) or set(data) != {
                "session", "session_dir", "version", "workspaces"
            }:
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            session = data.get("session")
            session_dir = data.get("session_dir")
            version = data.get("version")
            workspaces = data.get("workspaces")
            if (
                not isinstance(session, str) or not session
                or not isinstance(session_dir, str) or not session_dir
            ):
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            if type(version) is not int or version != 3:
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            if not isinstance(workspaces, list):
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            normalized_ws: list[dict[str, str]] = []
            for w in workspaces:
                if not isinstance(w, dict):
                    return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
                if set(w) != {"workspace_id", "identity_cwd"}:
                    return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
                wid = w["workspace_id"]
                cwd = w["identity_cwd"]
                if not isinstance(wid, str) or not isinstance(cwd, str) or not wid or not cwd:
                    return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
                normalized_ws.append({"workspace_id": wid, "identity_cwd": cwd})
            normalized_ws.sort(key=lambda x: (x["workspace_id"], x["identity_cwd"]))
            records.append(
                {
                    "session": session,
                    "session_dir": session_dir,
                    "version": version,
                    "workspaces": tuple(normalized_ws),
                }
            )
        return SourceReadResult(self.kind, "ok", records=tuple(records))


class CoordinationRunReader:
    """Read Coordination runs (mode=ro). Authority: run/participant state."""

    kind = "coordination_run"

    def read(self, roots: LegacyRoots) -> SourceReadResult:
        db = roots.coordination_db
        if db is None or not db.is_file():
            return SourceReadResult(self.kind, "unavailable", detail_code=SOURCE_NOT_FOUND)
        try:
            con = _connect_ro(db)
        except sqlite3.Error:
            return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
        try:
            fingerprint = tuple(
                tuple(r) for r in con.execute("PRAGMA table_info(runs)").fetchall()
            )
            if fingerprint != COORDINATION_RUNS_FINGERPRINT:
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            rows = con.execute(
                "SELECT run_id, project_key, session, session_dir, revision FROM runs"
            ).fetchall()
        except sqlite3.Error:
            return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
        finally:
            con.close()
        records: list[dict[str, Any]] = []
        for row in rows:
            run_id = row[0]
            project_key = row[1]
            session = row[2]
            session_dir = row[3]
            if type(row[4]) is not int:
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            revision = row[4]
            if not all(isinstance(v, str) for v in (run_id, project_key, session, session_dir)):
                return SourceReadResult(self.kind, "error", detail_code=SOURCE_CORRUPT)
            if not all(
                v
                for v in (run_id, project_key, session, session_dir)
            ):
                continue
            records.append(
                {
                    "run_id": run_id,
                    "project_key": project_key,
                    "session": session,
                    "session_dir": session_dir,
                    "revision": revision,
                }
            )
        return SourceReadResult(self.kind, "ok", records=tuple(records))


DEFAULT_READERS: tuple[LegacySourceReader, ...] = (
    AgentMailProjectReader(),
    MailProjectsJsonReader(),
    HerdrSessionReader(),
    CoordinationRunReader(),
)


# ── evidence builders (deterministic source_key/source_digest) ─────────────
def _agent_mail_evidence(rec: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    identity = {"project_id": int(rec["project_id"])}
    evidence = {
        "human_key": str(rec["human_key"]),
        "project_id": int(rec["project_id"]),
        "slug": str(rec["slug"]),
    }
    return identity, evidence, str(rec["human_key"])


def _mail_projects_evidence(rec: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    identity = {"session": rec["session"], "session_dir": rec["session_dir"]}
    evidence = {
        "project": rec["project"],
        "session": rec["session"],
        "session_dir": rec["session_dir"],
        "version": int(rec["version"]),
    }
    return identity, evidence, str(rec["project"])


def _herdr_evidence(rec: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]:
    identity = {"session": rec["session"], "session_dir": rec["session_dir"]}
    evidence = {
        "session": rec["session"],
        "session_dir": rec["session_dir"],
        "version": int(rec["version"]),
        "workspaces": [dict(w) for w in rec["workspaces"]],
    }
    cwds = tuple(sorted({str(w["identity_cwd"]) for w in rec["workspaces"]}))
    return identity, evidence, cwds


def _coordination_evidence(rec: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    identity = {"run_id": rec["run_id"]}
    evidence = {
        "project_key": rec["project_key"],
        "revision": int(rec["revision"]),
        "run_id": rec["run_id"],
        "session": rec["session"],
        "session_dir": rec["session_dir"],
    }
    return identity, evidence, str(rec["project_key"])


# ── candidate grouping (exact canonical path; no heuristic merge) ──────────
def build_candidates(
    reads: Mapping[str, SourceReadResult],
    *,
    local_boundary: Path | None,
) -> tuple[
    list[CandidateProject],
    list[SourceStatus],
    dict[str, dict[tuple[str, str], LegacySourceEvidence]],
]:
    """Group by exact canonical local path. Ownership sources: agent_mail /
    mail_projects / coordination. Herdr is observational: attaches to an existing
    candidate whose path matches a workspace identity_cwd; a session spanning
    multiple candidate paths is ambiguous and attaches to none."""
    source_kinds = (
        "agent_mail_project",
        "mail_projects_session",
        "herdr_session",
        "coordination_run",
    )
    status_by_kind: dict[str, SourceStatus] = {}
    for kind in source_kinds:
        r = reads.get(kind)
        if r is None:
            status_by_kind[kind] = SourceStatus(kind, "unavailable", SOURCE_NOT_FOUND)
        elif r.state == "ok":
            status_by_kind[kind] = SourceStatus(kind, "ok", None, len(r.records))
        else:
            status_by_kind[kind] = SourceStatus(kind, r.state, r.detail_code)

    def _mark_source_error(kind: str, code: str) -> None:
        current = status_by_kind[kind]
        if current.state != "error":
            status_by_kind[kind] = SourceStatus(kind, "error", code)

    # Normalize all evidence before grouping so identity conflicts cannot hide
    # behind different paths or source enumeration order.
    ownership: list[
        tuple[
            str,
            LegacySourceEvidence,
            tuple[str, str] | None,
            str | None,
        ]
    ] = []
    herdr_rows: list[
        tuple[tuple[str, ...], LegacySourceEvidence, tuple[str, str]]
    ] = []
    evidence_by_pair: dict[tuple[str, str], LegacySourceEvidence] = {}
    paths_by_pair: dict[tuple[str, str], set[str]] = {}
    conflicting_pairs: set[tuple[str, str]] = set()

    def _register_evidence(
        evidence: LegacySourceEvidence, paths: Sequence[str]
    ) -> None:
        pair = (evidence.source_kind, evidence.source_key)
        paths_by_pair.setdefault(pair, set()).update(paths)
        existing = evidence_by_pair.get(pair)
        if existing is None:
            evidence_by_pair[pair] = evidence
        elif existing.source_digest != evidence.source_digest:
            conflicting_pairs.add(pair)
            _mark_source_error(evidence.source_kind, EVIDENCE_CONFLICT)

    am = reads.get("agent_mail_project")
    if am and am.state == "ok":
        for rec in am.records:
            ident, ev, path = _agent_mail_evidence(rec)
            try:
                cpath = _validate_candidate_path(path, local_boundary)
            except _ImportError:
                continue
            evidence = LegacySourceEvidence(am.kind, _sha256_uri(ident), _sha256_uri(ev))
            _register_evidence(evidence, (cpath,))
            ownership.append((cpath, evidence, None, rec["slug"] or _slug_from_path(cpath)))

    mp = reads.get("mail_projects_session")
    if mp and mp.state == "ok":
        for rec in mp.records:
            ident, ev, path = _mail_projects_evidence(rec)
            try:
                cpath = _validate_candidate_path(path, local_boundary)
            except _ImportError:
                continue
            evidence = LegacySourceEvidence(mp.kind, _sha256_uri(ident), _sha256_uri(ev))
            _register_evidence(evidence, (cpath,))
            generation = (str(rec["session"]), str(rec["session_dir"]))
            ownership.append((cpath, evidence, generation, None))

    cr = reads.get("coordination_run")
    if cr and cr.state == "ok":
        for rec in cr.records:
            ident, ev, path = _coordination_evidence(rec)
            try:
                cpath = _validate_candidate_path(path, local_boundary)
            except _ImportError:
                continue
            evidence = LegacySourceEvidence(cr.kind, _sha256_uri(ident), _sha256_uri(ev))
            _register_evidence(evidence, (cpath,))
            generation = (str(rec["session"]), str(rec["session_dir"]))
            ownership.append((cpath, evidence, generation, None))

    hr = reads.get("herdr_session")
    if hr and hr.state == "ok":
        for rec in hr.records:
            ident, ev, cwds = _herdr_evidence(rec)
            canonical_cwds: list[str] = []
            for cwd in cwds:
                try:
                    canonical_cwds.append(_validate_candidate_path(cwd, local_boundary))
                except _ImportError:
                    continue
            evidence = LegacySourceEvidence(hr.kind, _sha256_uri(ident), _sha256_uri(ev))
            _register_evidence(evidence, canonical_cwds)
            generation = (str(rec["session"]), str(rec["session_dir"]))
            herdr_rows.append((tuple(sorted(set(canonical_cwds))), evidence, generation))

    blocked_paths = {
        path
        for pair in conflicting_pairs
        for path in paths_by_pair.get(pair, ())
    }

    generation_paths: dict[tuple[str, str], set[str]] = {}
    generation_kinds: dict[tuple[str, str], set[str]] = {}
    for cpath, evidence, generation, _slug in ownership:
        pair = (evidence.source_kind, evidence.source_key)
        if generation is None or pair in conflicting_pairs:
            continue
        generation_paths.setdefault(generation, set()).add(cpath)
        generation_kinds.setdefault(generation, set()).add(evidence.source_kind)
    for cwds, evidence, generation in herdr_rows:
        pair = (evidence.source_kind, evidence.source_key)
        if pair in conflicting_pairs or len(cwds) != 1:
            continue
        generation_paths.setdefault(generation, set()).add(cwds[0])
        generation_kinds.setdefault(generation, set()).add(evidence.source_kind)

    conflicting_generations = {
        generation
        for generation, paths in generation_paths.items()
        if len(paths) > 1
    }
    for generation in sorted(conflicting_generations):
        blocked_paths.update(generation_paths[generation])
        for kind in sorted(generation_kinds[generation]):
            _mark_source_error(kind, AUTHORITY_DISAGREEMENT)

    # path -> {(source_kind, source_key) -> evidence}; ownership sources create
    # groups, and every distinct provenance identity is retained.
    groups: dict[str, dict[tuple[str, str], LegacySourceEvidence]] = {}
    slug_choices: dict[str, dict[tuple[str, str], str]] = {}
    for cpath, evidence, generation, slug in ownership:
        pair = (evidence.source_kind, evidence.source_key)
        if (
            pair in conflicting_pairs
            or cpath in blocked_paths
            or generation in conflicting_generations
        ):
            continue
        groups.setdefault(cpath, {})[pair] = evidence
        if slug is not None:
            slug_choices.setdefault(cpath, {})[pair] = slug

    # Herdr observational: attach to existing candidate(s) matching identity_cwd.
    for cwds, evidence, generation in herdr_rows:
        pair = (evidence.source_kind, evidence.source_key)
        if pair in conflicting_pairs or generation in conflicting_generations:
            continue
        targets = [cpath for cpath in cwds if cpath in groups]
        if len(targets) != 1:
            # 0 -> no ownership candidate (Herdr cannot create); >1 -> ambiguous
            continue
        groups[targets[0]][pair] = evidence

    candidates: list[CandidateProject] = []
    for cpath in sorted(groups):
        ev_map = groups[cpath]
        if not ev_map:
            continue
        sources = tuple(ev_map[pair] for pair in sorted(ev_map))
        choices = slug_choices.get(cpath, {})
        slug = choices[min(choices)] if choices else _slug_from_path(cpath)
        candidates.append(
            CandidateProject(
                slug=slug,
                display_name=_basename(cpath),
                goal=None,
                node_id="local",
                canonical_path=cpath,
                vcs_kind="none",
                availability="unknown",
                sources=sources,
            )
        )
    return candidates, [status_by_kind[kind] for kind in source_kinds], groups


def _slug_from_path(path: str) -> str:
    base = path.rstrip("/").rsplit("/", 1)[-1]
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in base.lower()).strip("-")
    return cleaned or "project"


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate key")
        out[k] = v
    return out


# ── orchestrator ───────────────────────────────────────────────────────────
def import_legacy(
    store: project_registry_store.ProjectRegistryStore,
    roots: LegacyRoots,
    *,
    readers: Sequence[LegacySourceReader] | None = None,
) -> ImportReport:
    """Read legacy authorities read-only, normalize, and call the Store primitive
    once per candidate. Returns an ImportReport; never raises on candidate
    conflict (reported as CandidateOutcome)."""
    active_readers = tuple(readers) if readers is not None else DEFAULT_READERS
    reads: dict[str, SourceReadResult] = {}
    for reader in active_readers:
        reads[reader.kind] = reader.read(roots)

    candidates, sources_status, _groups = build_candidates(
        reads, local_boundary=roots.local_boundary
    )
    complete = all(s.state != "error" for s in sources_status)

    outcomes: list[CandidateOutcome] = []
    for cand in candidates:
        if not cand.sources:
            outcomes.append(CandidateOutcome(cand.canonical_path, "skipped", EMPTY_SOURCES))
            continue
        try:
            result = store.import_legacy_project(
                slug=cand.slug,
                display_name=cand.display_name,
                goal=cand.goal,
                node_id=cand.node_id,
                canonical_path=cand.canonical_path,
                vcs_kind=cand.vcs_kind,
                availability=cand.availability,
                sources=tuple(
                    domain.LegacySourceInput(
                        source_kind=s.source_kind,
                        source_key=s.source_key,
                        source_digest=s.source_digest,
                    )
                    for s in cand.sources
                ),
            )
        except project_registry_store.ProjectRegistryError as exc:
            outcomes.append(
                CandidateOutcome(cand.canonical_path, "conflict", exc.code or "legacy_import_conflict")
            )
            continue
        outcomes.append(
            CandidateOutcome(
                cand.canonical_path,
                "replayed" if result.replayed else "committed",
                None,
                result.replayed,
            )
        )
    return ImportReport(tuple(outcomes), tuple(sources_status), complete)


__all__ = [
    "canonical_json",
    "LegacyRoots",
    "LegacySourceEvidence",
    "CandidateProject",
    "SourceStatus",
    "CandidateOutcome",
    "ImportReport",
    "SourceReadResult",
    "LegacySourceReader",
    "AgentMailProjectReader",
    "MailProjectsJsonReader",
    "HerdrSessionReader",
    "CoordinationRunReader",
    "DEFAULT_READERS",
    "build_candidates",
    "import_legacy",
]
