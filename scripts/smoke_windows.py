"""Isolated Windows runner check using invented conversations only."""

import json
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def run() -> None:
    executable = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="dsh-import-smoke-") as directory:
        root = Path(directory)
        codex = root / "codex"
        rollout = codex / "sessions" / "2026" / "10" / "13" / "rollout-example.jsonl"
        rollout.parent.mkdir(parents=True)
        stamp = "2026-10-13T00:00:00.000Z"
        rows = [
            {"timestamp": stamp, "type": "session_meta", "payload": {"id": "sample-codex"}},
            {"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
            {"timestamp": stamp, "type": "response_item", "payload": {
                "type": "message", "role": "user", "id": "user-1",
                "content": [{"type": "input_text", "text": "Hello from a synthetic Codex chat"}]}},
            {"timestamp": stamp, "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "id": "assistant-1", "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Synthetic reply"}]}},
            {"timestamp": stamp, "type": "event_msg", "payload": {"type": "task_complete"}},
        ]
        rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        hermes = root / "hermes" / "state.db"
        hermes.parent.mkdir()
        with closing(sqlite3.connect(hermes)) as db:
            db.execute("CREATE TABLE sessions (id TEXT, title TEXT, started_at REAL, model TEXT, source TEXT)")
            db.execute("CREATE TABLE messages (id INTEGER, session_id TEXT, role TEXT, content TEXT, timestamp REAL, active INTEGER)")
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                       ("sample-hermes", "Synthetic Hermes chat", 1791849600, "sample", "cli"))
            db.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)", [
                (1, "sample-hermes", "user", "Hello from a synthetic Hermes chat", 1791849600, 1),
                (2, "sample-hermes", "assistant", "Synthetic reply", 1791849601, 1),
            ])
            db.commit()
        dsh = root / "dsh"
        registry = dsh / "storages" / "workspace.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({"tables": {"workspaces": {}},
                                        "global": {"workspaceIds": []}}), encoding="utf-8")
        common = [str(executable), "--dsh-home", str(dsh), "--codex-home", str(codex),
                  "--hermes-db", str(hermes), "--workspace-root", str(root / "imported")]
        subprocess.run(common, check=True)
        subprocess.run(common + ["--import"], check=True)
        subprocess.run(common + ["--verify"], check=True)
        subprocess.run(common + ["--import"], check=True)
        logs = list((dsh / "sessions").glob("*/*/session.v4.jsonl.zstd"))
        caches = list((dsh / "storages" / "session_projcache" / "sessions").glob("*.json"))
        workspaces = json.loads(registry.read_text(encoding="utf-8"))["tables"]["workspaces"]
        assert len(logs) == len(caches) == len(workspaces) == 2
        assert all(len(workspace["sessionIds"]) == 1 for workspace in workspaces.values())
        print("Windows smoke check passed: two chats, titles, projects, and idempotent rerun")


if __name__ == "__main__":
    run()
