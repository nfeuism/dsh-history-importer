#!/usr/bin/env python3
"""Import local Codex and Hermes text conversations into DeepSeek Harness.

Supports the on-disk v4 format used by DeepSeek Harness 0.2.0-rc.2.
Source databases and existing Harness sessions are never changed.
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

HOME = Path.home()
DSH_HOME = HOME / ".dsh"
SESSIONS_ROOT = DSH_HOME / "sessions"
WORKSPACE_JSON = DSH_HOME / "storages" / "workspace.json"

IMPORT_BASE = HOME / "Documents" / "deepseek-harness" / "imported"
CODEX_CWD = IMPORT_BASE / "codex-history"
HERMES_CWD = IMPORT_BASE / "hermes-history"

CODEX_HOME = HOME / ".codex"
CODEX_GLOBS = [
    str(CODEX_HOME / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl"),
    str(CODEX_HOME / "archived_sessions" / "rollout-*.jsonl"),
]
CODEX_INDEX = CODEX_HOME / "session_index.jsonl"

HERMES_DB = HOME / ".hermes" / "state.db"

CACHE_ROOT = DSH_HOME / "storages" / "session_projcache" / "sessions"

NAMESPACE = uuid.UUID("6f1d0f6e-6b0a-4f5a-9d3f-2d1f0a7c4b21")

# DSH writes its frames with the Zstandard content checksum enabled.
ZSTD_BINS = ("zstd", "/opt/homebrew/bin/zstd", "/usr/local/bin/zstd")

# --------------------------------------------------------------------------
# DSH path encoding (faithful ports from dsh-session-persistence-jsonl)
# --------------------------------------------------------------------------

_SAFE = re.compile(r"^[A-Za-z0-9._-]$")


def _utf16_chars(value: str):
    raw = value.encode("utf-16-le", "surrogatepass")
    for i in range(0, len(raw), 2):
        yield chr(int.from_bytes(raw[i:i + 2], "little"))


def project_key(cwd: str) -> str:
    """Port of projectKey(): human-navigable, bounded, filesystem-safe."""
    if not cwd:
        raise ValueError("cannot encode an empty project path")
    readable = ""
    separator_run = False
    for ch in _utf16_chars(cwd):
        code = ord(ch)
        if ch in ("/", "\\", ":"):
            if not separator_run:
                readable += "-"
            separator_run = True
        elif ch != "~" and _SAFE.match(ch):
            readable += ch
            separator_run = False
        else:
            readable += "~" + format(code, "04X")
            separator_run = False
    stripped = re.sub(r"^-+", "", readable) or "root"
    return "--" + stripped[:251] + "--"


def encode_segment(raw: str) -> str:
    """Port of encodeSegment(): one safe path segment per session id."""
    if not raw:
        raise ValueError("cannot encode an empty path segment")
    if raw == ".":
        return "~002E"
    if raw == "..":
        return "~002E~002E"
    out = ""
    for ch in _utf16_chars(raw):
        if ch != "~" and _SAFE.match(ch):
            out += ch
        else:
            out += "~" + format(ord(ch), "04X")
    return out


def session_log_path(cwd: str, session_id: str) -> Path:
    return SESSIONS_ROOT / project_key(cwd) / encode_segment(session_id) / "session.v4.jsonl.zstd"


# --------------------------------------------------------------------------
# DSH v4 session log emitter
# --------------------------------------------------------------------------
#
# A v4 log is: one header record, then one record per durable event.
#   header: {"type":"session","version":4,"id","createdAt","cwd",
#            "isSeeded":false,"delegationDepth":0}
#   event : {"type","seq","time","data"[, "surfaceOp" | "sourceEventSeqs"]}
#
# Only the events needed for a readable transcript are emitted. Every message
# surface event carries surfaceOp:"append". assistant/message additionally
# requires turn/step and a `stream` array (DSH's settlement check demands it,
# even when empty). These invariants were confirmed against DSH's own
# publication-grade validator (`validation:'current'`, `recovery:'strict'`).
# --------------------------------------------------------------------------


class SessionBuilder:
    def __init__(self, session_id: str, cwd: str, created_at_ms: int):
        self.session_id = session_id
        self.cwd = cwd
        self.created_at = int(created_at_ms)
        self.rows: list[dict] = []
        self.seq = 0
        self.turn = 0
        self.last_time = self.created_at
        self.first_user_seq: int | None = None
        self.keywords: list[str] = []

    # -- internals ---------------------------------------------------------

    def _time(self, ms: int | None) -> int:
        """Times are safe integers and kept non-decreasing."""
        if ms is None:
            ms = self.last_time + 1
        ms = int(ms)
        if ms < self.last_time:
            ms = self.last_time
        self.last_time = ms
        return ms

    def _add(self, etype: str, data: dict, time_ms: int | None = None, **extra) -> int:
        seq = self.seq
        row = {"type": etype, "seq": seq, "time": self._time(time_ms), "data": data}
        row.update(extra)
        self.rows.append(row)
        self.seq += 1
        return seq

    # -- public ------------------------------------------------------------

    def add_turn(
        self,
        user_text: str | None,
        assistant_text: str | None,
        user_time_ms: int | None,
        assistant_time_ms: int | None,
        provider: str,
        model: str,
    ) -> None:
        """One DSH turn = one user prompt plus the assistant reply to it."""
        if not (user_text or assistant_text):
            return
        self.turn += 1
        t = self.turn
        self._add("turn/start", {"turn": t}, user_time_ms)
        if user_text:
            u_seq = self._add(
                "user/message",
                {
                    "content": [{"type": "text", "text": user_text}],
                    "source": {"kind": "user"},
                    "role": "user",
                    "id": str(uuid.uuid4()),
                },
                user_time_ms,
                surfaceOp="append",
            )
            if self.first_user_seq is None:
                self.first_user_seq = u_seq
                self.keywords = _title_keywords(user_text)
        self._add("step/start", {"turn": t, "step": 1}, user_time_ms)
        if assistant_text:
            self._add(
                "assistant/message",
                {
                    "turn": t,
                    "step": 1,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": assistant_text}],
                        "source": {
                            "kind": "model",
                            "provider": provider,
                            "model": model,
                        },
                        "id": str(uuid.uuid4()),
                    },
                    "stream": [],
                },
                assistant_time_ms,
                surfaceOp="append",
            )
        self._add("step/end", {"turn": t, "step": 1}, assistant_time_ms)
        self._add("turn/end", {"turn": t, "reason": {"kind": "completed"}}, assistant_time_ms)

    def add_title(self, title: str, derived: bool) -> None:
        """`derived` titles cite the first user message; stored names do not."""
        title = _clean_title(title)
        if not title:
            return
        if derived and self.first_user_seq is not None:
            self._add(
                "session/title",
                {
                    "title": title,
                    "messageSeqs": [self.first_user_seq],
                    "source": {"kind": "fallback"},
                },
            )
        else:
            self._add(
                "session/title",
                {"title": title, "messageSeqs": [], "source": {"kind": "user"}},
            )

    def to_jsonl(self) -> str:
        header = {
            "type": "session",
            "version": 4,
            "id": self.session_id,
            "createdAt": self.created_at,
            "cwd": self.cwd,
            "isSeeded": False,
            "delegationDepth": 0,
        }
        lines = [json.dumps(header, ensure_ascii=False, separators=(",", ":"))]
        for row in self.rows:
            lines.append(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return "\n".join(lines) + "\n"


def _clean_title(text: str, limit: int = 60) -> str:
    t = " ".join((text or "").split())
    return t[:limit]


def _title_keywords(text: str) -> list[str]:
    return re.findall(r"\w+", text)[:12]


# --------------------------------------------------------------------------
# Conversation model
# --------------------------------------------------------------------------


class Conversation:
    __slots__ = ("source", "key", "title", "title_derived", "created_ms", "provider",
                 "model", "turns", "sid_override")

    def __init__(self, source, key, title, title_derived, created_ms, provider, model):
        self.source = source
        self.key = key
        self.title = title
        self.title_derived = title_derived
        self.created_ms = created_ms
        self.provider = provider
        self.model = model
        self.turns: list[dict] = []
        self.sid_override: str | None = None

    def session_id(self) -> str:
        if self.sid_override:
            return self.sid_override
        return "session-" + str(uuid.uuid5(NAMESPACE, f"{self.source}:{self.key}"))


# --------------------------------------------------------------------------
# Codex extraction
# --------------------------------------------------------------------------

# Known framework wrappers that are not part of a user's prompt.
CODEX_BOILERPLATE = (
    "<permissions instructions>", "<environment_context", "# AGENTS.md instructions",
    "<recommended_plugins>", "<skills_instructions>", "<apps_instructions>",
    "<plugins_instructions>", "<multi_agent_mode>", "<multi_agent_role>",
    "<collaboration_mode>", "<app-context>", "<user_instructions",
    "<turn_aborted>", "<heartbeat>", "<in-app-browser-context",
    "<external_codex_apps_open_page>", "<image_resize_notice>",
    "<model_switch>", "<codex_apps_open_page_instructions>",
    "<codex_apps_client_time_context>", "<skill>", "<tool_result",
    "<subagent_notification>",
    "## Referenced ChatGPT conversation:", "# Diff comments:",
    "# Files mentioned by the user:", "# Applications mentioned by the user:",
    "\nSome conversation entries were omitted.",
)


def _is_boilerplate(text: str) -> bool:
    t = text.lstrip()
    return any(t.startswith(p) for p in CODEX_BOILERPLATE)


def _is_machine_payload(text: str) -> bool:
    """Known Codex approval reply; preserve other JSON assistant answers."""
    s = text.strip()
    if not s or s[0] not in "{[":
        return False
    try:
        value = json.loads(s)
    except Exception:
        return False
    return (isinstance(value, dict) and isinstance(value.get("outcome"), str)
            and value["outcome"] in {"allow", "deny"}
            and set(value).issubset({"outcome", "reason", "message"}))


def _parse_iso(ts: str) -> int:
    """ISO-8601 with millisecond precision, UTC -> epoch ms."""
    try:
        dt = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=_dt.timezone.utc
        )
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _iter_jsonl(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def codex_thread_names() -> dict[str, str]:
    names: dict[str, str] = {}
    if not CODEX_INDEX.exists():
        return names
    for rec in _iter_jsonl(str(CODEX_INDEX)):
        tid, name = rec.get("id"), rec.get("thread_name")
        if tid and name:
            names[tid] = name
    return names


def extract_codex(limit: int | None = None) -> tuple[list[Conversation], dict]:
    stats = collections.Counter()
    files: list[str] = []
    for pattern in CODEX_GLOBS:
        files.extend(glob.glob(pattern))
    files.sort()

    # Group files by root conversation id (session_meta.session_id).
    threads: dict[str, list] = collections.defaultdict(list)
    for path in files:
        recs = list(_iter_jsonl(path))
        if not recs:
            stats["files_unreadable"] += 1
            continue
        meta = next((r.get("payload") or {} for r in recs if r.get("type") == "session_meta"), {})
        root = meta.get("session_id") or meta.get("id") or os.path.basename(path)
        threads[root].append((recs[0].get("timestamp") or "", path, meta, recs))
        stats["files"] += 1

    names = codex_thread_names()
    out: list[Conversation] = []

    for root, group in threads.items():
        group.sort(key=lambda g: g[0])
        recs: list[dict] = []
        for _, _, _, rs in group:
            recs.extend(rs)

        created_ms = _parse_iso(group[0][0]) or _parse_iso(
            (group[0][2] or {}).get("timestamp", "")
        )

        turns_raw, model_default = _codex_turns(recs, stats)
        turns = _coalesce_user_turns(_codex_segments(turns_raw))
        if not turns:
            stats["threads_empty"] += 1
            continue

        title = names.get(root) or ""
        derived = not title
        if not title:
            title = turns[0]["user"]

        conv = Conversation(
            source="codex",
            key=root,
            title=title,
            title_derived=derived,
            created_ms=created_ms,
            provider="codex-desktop",
            model=model_default or "codex",
        )
        conv.turns = turns
        out.append(conv)

    out.sort(key=lambda c: c.created_ms)
    if limit:
        out = out[-limit:]
    return out, dict(stats)


def _coalesce_user_turns(turns: list[dict]) -> list[dict]:
    """Merge a repeated identical prompt into the turn it repeats.

    Codex re-records the same prompt on some continuation turns, which would
    otherwise surface as two consecutive identical user messages.
    """
    out: list[dict] = []
    for t in turns:
        if out and t["user"] and t["user"] == out[-1]["user"]:
            prev = out[-1]
            if t.get("assistant"):
                prev["assistant"] = (
                    prev["assistant"] + "\n\n" + t["assistant"]
                    if prev.get("assistant")
                    else t["assistant"]
                )
            prev["assistant_ms"] = t.get("assistant_ms") or prev.get("assistant_ms")
            continue
        out.append(dict(t))
    return out


def _codex_segments(turns_raw: list[dict]) -> list[dict]:
    """Collapse Codex's internal task turns into one turn per user prompt.

    Codex opens a fresh `task_started` turn for every internal task, so one
    user prompt routinely spans several turns (continuations, delegated
    sub-tasks). A DSH turn is "one prompt plus its reply", so the segment's
    answers are joined in order; segments before the first genuine user
    message are internal-only and are dropped.
    """
    segments: list[dict] = []
    cur: dict | None = None

    for tn in turns_raw:
        user = "\n\n".join(x for x in tn["user"] if x).strip()
        answer = (tn.get("final_answer") or "").strip()

        if user:
            cur = {
                "user": [user],
                "assistant": [],
                "user_ms": tn.get("ts_start_ms"),
                "assistant_ms": None,
            }
            segments.append(cur)
        elif cur is None:
            continue

        if answer and answer not in cur["assistant"]:
            cur["assistant"].append(answer)
            cur["assistant_ms"] = tn.get("ts_end_ms") or cur["assistant_ms"]

    out = []
    for seg in segments:
        out.append(
            {
                "user": "\n\n".join(seg["user"]).strip(),
                "assistant": "\n\n".join(seg["assistant"]).strip(),
                "user_ms": seg["user_ms"],
                "assistant_ms": seg["assistant_ms"] or seg["user_ms"],
            }
        )
    return [t for t in out if t["user"] or t["assistant"]]


def _codex_turns(recs: list[dict], stats) -> tuple[list[dict], str | None]:
    """Replay one logical thread's records into turns.

    Messages are de-duplicated by their own id, which is what collapses the
    re-included history that continuation files carry.
    """
    turns: list[dict] = []
    cur: dict | None = None
    seen_msg: set[str] = set()
    model_default: str | None = None
    seen_turns: set[str] = set()

    for r in recs:
        rtype = r.get("type")
        p = r.get("payload") or {}
        pt = p.get("type")
        ts = r.get("timestamp")

        if rtype == "turn_context":
            model_default = p.get("model") or model_default
            continue

        if rtype == "event_msg" and pt == "task_started":
            tid = p.get("turn_id")
            if tid and tid in seen_turns:
                cur = None          # replay of an already-imported turn
                stats["turns_deduped"] += 1
                continue
            if tid:
                seen_turns.add(tid)
            cur = {
                "turn_id": tid,
                "ts_start_ms": _parse_iso(ts) if ts else None,
                "ts_end_ms": None,
                "user": [],
                "assistant": [],
                "final_answer": None,
            }
            turns.append(cur)
            stats["turns"] += 1
            continue

        if rtype == "event_msg" and pt in ("task_complete", "turn_aborted"):
            if cur is not None:
                if ts:
                    cur["ts_end_ms"] = _parse_iso(ts)
                cur = None
            continue

        if rtype != "response_item" or pt != "message":
            continue

        mid = p.get("id")
        if mid:
            if mid in seen_msg:
                stats["messages_deduped"] += 1
                continue
            seen_msg.add(mid)

        role = p.get("role")
        if role == "developer":
            continue
        content = p.get("content") or []
        text = "".join(
            ci.get("text", "")
            for ci in content
            if isinstance(ci, dict) and ci.get("type") in ("input_text", "output_text", "text")
        )
        images = sum(
            1 for ci in content if isinstance(ci, dict) and ci.get("type") == "input_image"
        )
        target = cur if cur is not None else (turns[-1] if turns else None)
        if target is None:
            continue

        if role == "user":
            if _is_boilerplate(text):
                stats["user_boilerplate_dropped"] += 1
                continue
            if not text.strip():
                continue
            if images:
                text = text.rstrip() + f"\n\n[原会话含 {images} 张图片附件，未导入]"
            if not target["user"]:
                target["ts_start_ms"] = target["ts_start_ms"] or (_parse_iso(ts) if ts else None)
            target["user"].append(text)
            stats["user_messages"] += 1
        elif role == "assistant":
            if not text.strip():
                continue
            if _is_machine_payload(text):
                stats["assistant_machine_payload_dropped"] += 1
                continue
            target["assistant"].append({"text": text, "phase": p.get("phase"), "ts": ts})
            stats["assistant_messages"] += 1

    for tn in turns:
        final = next(
            (a["text"] for a in reversed(tn["assistant"]) if a.get("phase") == "final_answer"),
            None,
        )
        if final is None:
            final = tn["assistant"][-1]["text"] if tn["assistant"] else None
        tn["final_answer"] = final
        if tn["ts_end_ms"] is None and tn["assistant"]:
            tn["ts_end_ms"] = _parse_iso(tn["assistant"][-1]["ts"]) if tn["assistant"][-1]["ts"] else None

    return turns, model_default


# --------------------------------------------------------------------------
# Hermes extraction
# --------------------------------------------------------------------------


def _hermes_connection(snapshot_dir: Path) -> sqlite3.Connection:
    """Snapshot Hermes' live SQLite database through the online-backup API."""
    if not HERMES_DB.exists():
        raise SystemExit(f"Hermes database not found: {HERMES_DB}")
    src = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True, timeout=15)
    snapshot = snapshot_dir / "hermes.db"
    dst = sqlite3.connect(snapshot)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    conn = sqlite3.connect(snapshot)
    conn.row_factory = sqlite3.Row
    return conn


def _hermes_is_noise(role: str, content: str) -> bool:
    """Skip known context compaction notes, keeping normal bracketed prompts."""
    c = (content or "").lstrip()
    if role in ("user", "assistant") and (
        c.startswith("[CONTEXT COMPACTION") or c.startswith("[PRIOR CONTEXT")
    ):
        return True
    return False


def extract_hermes(limit: int | None = None) -> tuple[list[Conversation], dict]:
    stats = collections.Counter()
    with tempfile.TemporaryDirectory(prefix="dsh-hermes-") as snapshot_dir:
        return _extract_hermes_snapshot(Path(snapshot_dir), limit, stats)


def _extract_hermes_snapshot(snapshot_dir: Path, limit: int | None,
                             stats: collections.Counter) -> tuple[list[Conversation], dict]:
    conn = _hermes_connection(snapshot_dir)
    cur = conn.cursor()

    sessions = cur.execute(
        """
        SELECT s.id, s.title, s.started_at, s.model, s.source
        FROM sessions s
        WHERE EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id AND m.active = 1)
        ORDER BY s.started_at
        """
    ).fetchall()

    out: list[Conversation] = []
    for s in sessions:
        rows = cur.execute(
            """
            SELECT id, role, content, timestamp
            FROM messages
            WHERE session_id = ? AND active = 1
            ORDER BY id
            """,
            (s["id"],),
        ).fetchall()

        turns: list[dict] = []
        cur_turn: dict | None = None
        for row in rows:
            role = row["role"]
            if role not in ("user", "assistant"):
                continue
            content = (row["content"] or "").strip()
            if not content:
                continue
            if _hermes_is_noise(role, content):
                stats["noise_dropped"] += 1
                continue
            ts_ms = int(float(row["timestamp"]) * 1000) if row["timestamp"] else None
            if role == "user":
                cur_turn = {"user": content, "assistant": "", "user_ms": ts_ms, "assistant_ms": None}
                turns.append(cur_turn)
                stats["user_messages"] += 1
            else:
                if cur_turn is None:
                    cur_turn = {"user": "", "assistant": "", "user_ms": ts_ms, "assistant_ms": None}
                    turns.append(cur_turn)
                cur_turn["assistant"] = (
                    cur_turn["assistant"] + "\n\n" + content if cur_turn["assistant"] else content
                )
                cur_turn["assistant_ms"] = ts_ms
                stats["assistant_messages"] += 1

        turns = _coalesce_user_turns([t for t in turns if t["user"] or t["assistant"]])
        if not turns:
            stats["sessions_empty"] += 1
            continue

        title = (s["title"] or "").strip()
        derived = not title
        if not title:
            title = turns[0]["user"]

        model = (s["model"] or "").strip() or "hermes"
        conv = Conversation(
            source="hermes",
            key=s["id"],
            title=title,
            title_derived=derived,
            created_ms=int(float(s["started_at"]) * 1000) if s["started_at"] else 0,
            provider="hermes-agent",
            model=model,
        )
        conv.turns = turns
        out.append(conv)

    conn.close()
    out.sort(key=lambda c: c.created_ms)
    if limit:
        out = out[-limit:]
    return out, dict(stats)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def find_zstd() -> str:
    for cand in ZSTD_BINS:
        path = shutil.which(cand) if "/" not in cand else (cand if os.path.exists(cand) else None)
        if path:
            return path
    raise SystemExit("zstd not found; install it (brew install zstd) or add it to PATH")


def write_conversation(conv: Conversation, cwd: Path) -> tuple[Path, bool]:
    """Returns (path, written)."""
    cwd.mkdir(parents=True, exist_ok=True)
    sid = conv.session_id()
    target = session_log_path(str(cwd), sid)
    builder = SessionBuilder(sid, str(cwd), conv.created_ms)
    for t in conv.turns:
        builder.add_turn(
            t["user"],
            t["assistant"],
            t.get("user_ms"),
            t.get("assistant_ms"),
            conv.provider,
            conv.model,
        )
    builder.add_title(conv.title, conv.title_derived)
    if target.exists():
        verify_log(target, sid)
        write_cache(builder)
        return target, False
    payload = builder.to_jsonl().encode("utf-8")

    target.parent.mkdir(parents=True, exist_ok=True)
    zstd = find_zstd()
    # DSH requires the header line to occupy its own Zstandard frame.
    # Its session lister reads only that first frame during startup.
    header, body = payload.split(b"\n", 1)
    if not body:
        raise SystemExit(f"session {sid} has no event rows")

    def frame(data: bytes) -> bytes:
        proc = subprocess.run(
            [zstd, "--check", "-q", "-c"],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise SystemExit(f"zstd failed: {proc.stderr.decode('utf-8', 'replace')}")
        return proc.stdout

    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".session-", suffix=".tmp",
                                     delete=False) as fh:
        tmp = Path(fh.name)
        fh.write(frame(header + b"\n") + frame(body))
        fh.flush()
        os.fsync(fh.fileno())
    try:
        if target.exists():
            verify_log(target, sid)
            return target, False
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    verify_log(target, sid)
    write_cache(builder)
    return target, True


# --------------------------------------------------------------------------
# Read-back verification. This catches the format mistakes that previously
# made sessions invisible in the sidebar or blank in the conversation view.
# --------------------------------------------------------------------------


def verify_log(path: Path, expected_id: str) -> None:
    zstd = find_zstd()
    frame_info = subprocess.run([zstd, "-lv", str(path)], capture_output=True, text=True,
                                check=True)
    if not re.search(r"# Zstandard Frames:\s*2(?:\D|$)", frame_info.stdout + frame_info.stderr):
        raise ValueError(f"DSH requires exactly two Zstandard frames: {path}")
    result = subprocess.run([zstd, "-d", "-q", "-c", str(path)],
                            capture_output=True, check=True)
    rows = [json.loads(line) for line in result.stdout.splitlines() if line]
    if not rows or rows[0].get("type") != "session" or rows[0].get("version") != 4:
        raise ValueError(f"invalid session header: {path}")
    if rows[0].get("id") != expected_id:
        raise ValueError(f"session ID mismatch: {path}")
    events = rows[1:]
    if not events or [r.get("seq") for r in events] != list(range(len(events))):
        raise ValueError(f"missing or out-of-order events: {path}")
    if not any(r.get("type") == "turn/end" for r in events):
        raise ValueError(f"no conversation turns: {path}")
    for r in events:
        if r.get("type") == "turn/end" and r.get("data", {}).get("reason") != {"kind": "completed"}:
            raise ValueError(f"turn cannot display in chat: {path}")
    for r in events:
        if r.get("type") in ("user/message", "assistant/message") and r.get("surfaceOp") != "append":
            raise ValueError(f"message surface operation missing: {path}")


def cache_record(builder: SessionBuilder) -> dict:
    title_events = [r for r in builder.rows if r["type"] == "session/title"]
    user_events = [r for r in builder.rows if r["type"] == "user/message"]
    rows = {
        "sessionListMetadata": {
            "ver": 1, "seq": builder.seq - 1,
            "val": {"blank": False, "lastPromptAt": user_events[-1]["time"] if user_events else None},
        }
    }
    if title_events:
        rows["title"] = {"ver": 1, "seq": title_events[-1]["seq"],
                         "val": title_events[-1]["data"]["title"]}
    return {
        "version": 7,
        "record": {
            "identity": {"formatVersion": 4, "createdAt": builder.created_at,
                         "cwd": builder.cwd, "isSeeded": False, "inheritedEventCount": 0},
            "rows": rows,
        },
    }


def write_cache(builder: SessionBuilder) -> bool:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    target = CACHE_ROOT / f"{builder.session_id}.json"
    if target.exists():
        return False
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_ROOT,
                                     prefix=".cache-", suffix=".tmp", delete=False) as fh:
        tmp = Path(fh.name)
        json.dump(cache_record(builder), fh, ensure_ascii=False, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    try:
        if target.exists():
            return False
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return True


# --------------------------------------------------------------------------
# Registration into workspace.json
# --------------------------------------------------------------------------


def dsh_is_running() -> bool:
    proc = subprocess.run(["pgrep", "-f", "DeepSeek Harness"], capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        raise SystemExit("Cannot determine whether DeepSeek Harness is running; close it and retry")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def register(convs_by_source: dict[str, list[Conversation]]) -> None:
    if dsh_is_running():
        raise SystemExit(
            "DeepSeek Harness is running. Quit it first so its in-memory workspace\n"
            "registry cannot overwrite this change, then retry."
        )
    if not WORKSPACE_JSON.exists():
        raise SystemExit(f"workspace registry not found: {WORKSPACE_JSON}")

    state = json.loads(WORKSPACE_JSON.read_text(encoding="utf-8"))
    tables = state.setdefault("tables", {}).setdefault("workspaces", {})
    glob = state.setdefault("global", {})
    order: list[str] = glob.setdefault("workspaceIds", [])

    by_path = {rec["path"]: wid for wid, rec in tables.items()}
    owners: dict[str, str] = {}
    for wid, rec in tables.items():
        for sid in rec.get("sessionIds", []):
            owners[sid] = wid

    plans = [
        ("codex", CODEX_CWD, "Codex 历史"),
        ("hermes", HERMES_CWD, "Hermes 历史"),
    ]
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )

    changed = False
    for source, cwd, title in plans:
        convs = convs_by_source.get(source) or []
        if not convs:
            continue
        path = os.path.realpath(str(cwd))
        ids = [c.session_id() for c in convs]
        ids.sort(key=lambda sid: -_created_of(convs, sid))

        wid = by_path.get(path)
        if wid is None:
            wid = str(uuid.uuid4())
            tables[wid] = {
                "path": path,
                "title": title,
                "sessionIds": [],
                "createdAt": now_iso,
                "updatedAt": now_iso,
            }
            order.append(wid)
            by_path[path] = wid
            print(f"  + project '{title}' -> {path}")
        else:
            print(f"  = project already registered: {path}")

        rec = tables[wid]
        # One session may belong to exactly one workspace.
        for sid in ids:
            other = owners.get(sid)
            if other is not None and other != wid:
                raise SystemExit(f"session {sid} already accounted by workspace {other}")
        present = rec.get("sessionIds", [])
        added = [sid for sid in ids if sid not in present]
        if added:
            rec["sessionIds"] = present + added
            rec["updatedAt"] = now_iso
            changed = True
        print(f"    {len(added)} sessions added")

    if not changed:
        print("  nothing to register")
        return

    # Invariants the registry validates loudly at startup.
    if set(order) != set(tables):
        missing = set(tables) - set(order)
        order.extend(sorted(missing))
    all_sids: list[str] = []
    for rec in tables.values():
        all_sids.extend(rec.get("sessionIds", []))
    dupes = [s for s, n in collections.Counter(all_sids).items() if n > 1]
    if dupes:
        raise SystemExit(f"refusing to write: duplicate session accounts {dupes[:5]}")
    paths = [rec["path"] for rec in tables.values()]
    if len(set(paths)) != len(paths):
        raise SystemExit("refusing to write: duplicate workspace paths")

    backup = WORKSPACE_JSON.with_name(
        f"workspace.json.bak.{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.{uuid.uuid4().hex[:8]}"
    )
    shutil.copy2(WORKSPACE_JSON, backup)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=WORKSPACE_JSON.parent,
                                     prefix=".workspace-", suffix=".tmp", delete=False) as fh:
        tmp = Path(fh.name)
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.replace(tmp, WORKSPACE_JSON)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"  workspace.json updated (backup: {backup.name})")


def _created_of(convs: list[Conversation], sid: str) -> int:
    for c in convs:
        if c.session_id() == sid:
            return c.created_ms
    return 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def configure_paths(args: argparse.Namespace) -> None:
    global DSH_HOME, SESSIONS_ROOT, WORKSPACE_JSON, CACHE_ROOT
    global IMPORT_BASE, CODEX_CWD, HERMES_CWD, CODEX_HOME, CODEX_GLOBS, CODEX_INDEX, HERMES_DB
    DSH_HOME = args.dsh_home.expanduser().resolve()
    SESSIONS_ROOT = DSH_HOME / "sessions"
    WORKSPACE_JSON = DSH_HOME / "storages" / "workspace.json"
    CACHE_ROOT = DSH_HOME / "storages" / "session_projcache" / "sessions"
    IMPORT_BASE = args.workspace_root.expanduser().resolve()
    CODEX_CWD = IMPORT_BASE / "codex-history"
    HERMES_CWD = IMPORT_BASE / "hermes-history"
    CODEX_HOME = args.codex_home.expanduser().resolve()
    CODEX_GLOBS = [
        str(CODEX_HOME / "sessions" / "*" / "*" / "*" / "rollout-*.jsonl"),
        str(CODEX_HOME / "archived_sessions" / "rollout-*.jsonl"),
    ]
    CODEX_INDEX = CODEX_HOME / "session_index.jsonl"
    HERMES_DB = args.hermes_db.expanduser().resolve()


def find_existing_session(session_id: str) -> Path | None:
    if not SESSIONS_ROOT.exists():
        return None
    candidates = list(SESSIONS_ROOT.glob(f"*/{encode_segment(session_id)}/session.v4.jsonl.zstd"))
    if len(candidates) > 1:
        raise SystemExit(f"Duplicate Harness session ID already exists: {session_id}")
    return candidates[0] if candidates else None


def report(label: str, convs: list[Conversation], stats: dict) -> None:
    turns = sum(len(c.turns) for c in convs)
    print(f"{label}: {len(convs)} conversations, {turns} turns")
    if stats.get("files_unreadable"):
        print(f"  warning: {stats['files_unreadable']} Codex files were unreadable")


def main() -> int:
    ap = argparse.ArgumentParser(description="Import Codex/Hermes text history into DeepSeek Harness")
    ap.add_argument("--source", choices=["codex", "hermes", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true", help="preview only (default)")
    ap.add_argument("--import", dest="do_import", action="store_true", help="write and register sessions")
    ap.add_argument("--verify", action="store_true", help="check imported logs without changing them")
    ap.add_argument("--limit", type=int, help="newest N conversations per source")
    ap.add_argument("--dsh-home", type=Path, default=Path.home() / ".dsh")
    ap.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    ap.add_argument("--hermes-db", type=Path, default=Path.home() / ".hermes" / "state.db")
    ap.add_argument("--workspace-root", type=Path,
                    default=Path.home() / "Documents" / "deepseek-harness" / "imported")
    args = ap.parse_args()
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be positive")
    if args.do_import and args.dry_run:
        ap.error("choose --import or --dry-run")
    if args.do_import and args.verify:
        ap.error("run --verify separately after import")
    configure_paths(args)

    if not DSH_HOME.exists() or not WORKSPACE_JSON.exists():
        raise SystemExit(f"DeepSeek Harness has not initialized {DSH_HOME}; start it once, then quit")
    selected = ["codex", "hermes"] if args.source == "all" else [args.source]
    convs_by_source: dict[str, list[Conversation]] = {}
    for source in selected:
        if source == "codex":
            convs, stats = extract_codex(args.limit)
        elif HERMES_DB.exists():
            convs, stats = extract_hermes(args.limit)
        elif args.source == "hermes":
            raise SystemExit(f"Hermes database not found: {HERMES_DB}")
        else:
            print(f"Hermes database not found; skipping: {HERMES_DB}")
            continue
        convs_by_source[source] = convs
        report(source, convs, stats)

    planned: dict[str, list[Conversation]] = {}
    for source, convs in convs_by_source.items():
        cwd = CODEX_CWD if source == "codex" else HERMES_CWD
        for conv in convs:
            expected = session_log_path(str(cwd), conv.session_id())
            existing = find_existing_session(conv.session_id())
            if existing and existing != expected:
                raise SystemExit(
                    f"Session {conv.session_id()} already exists in another workspace: {existing}. "
                    "Use the original --workspace-root; no duplicate will be created."
                )
        planned[source] = convs
    count = sum(len(v) for v in planned.values())
    if not args.do_import and not args.verify:
        print(f"Preview: {count} conversations; nothing written. Quit Harness, then run --import.")
        return 0
    if args.verify:
        good = 0
        for source, convs in planned.items():
            cwd = CODEX_CWD if source == "codex" else HERMES_CWD
            for conv in convs:
                path = session_log_path(str(cwd), conv.session_id())
                if not path.exists():
                    raise SystemExit(f"Missing imported session: {path}")
                verify_log(path, conv.session_id())
                good += 1
        print(f"Verified {good} imported conversations.")
        return 0
    if dsh_is_running():
        raise SystemExit("Quit DeepSeek Harness completely before importing, then retry")
    if not count:
        print("No conversations found; nothing written.")
        return 0
    written = 0
    skipped = 0
    for source, convs in planned.items():
        cwd = CODEX_CWD if source == "codex" else HERMES_CWD
        for conv in convs:
            _, did_write = write_conversation(conv, cwd)
            written += int(did_write)
            skipped += int(not did_write)
    register(planned)
    print(f"Import complete: {written} new, {skipped} already present. Reopen DeepSeek Harness.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, sqlite3.Error, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Import stopped: {exc}") from exc
