"""Persistent memory layer for the agent system.

Layout under ``<project>/memory/``::

    state/        # latest JSON snapshots per category (current bot state, last cycle)
    history/      # JSONL append-only history (cycles, trades, anomalies)
    lessons/      # Markdown write-only-by-agents (Reflector writes here)
    proposals/    # Evolver proposals awaiting human / auto approval
    health/       # Healer reports
    journal/      # every agent's notable decisions (free-form Markdown)

Every write is atomic (tmp -> os.replace) so a crash mid-write does not
leave a corrupt JSON file. Reads tolerate missing files (return {} or
empty list) so a fresh deploy starts cleanly.

The whole tree is gitignored; it lives on disk only.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO)


@dataclass
class Memory:
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self._lock = threading.Lock()
        for sub in ("state", "history", "lessons", "proposals", "health", "journal"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # state/   — latest JSON snapshot per key
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_name(key: str) -> str:
        # Replace OS-illegal chars (Windows reserves < > : " / \ | ? *)
        return "".join(c if c.isalnum() or c in "._-+" else "_" for c in key)

    def write_state(self, key: str, payload: dict[str, Any]) -> None:
        path = self.root / "state" / f"{self._safe_name(key)}.json"
        payload = {**payload, "_updated_at": now_iso()}
        self._write_json_atomic(path, payload)

    def read_state(self, key: str) -> dict[str, Any]:
        path = self.root / "state" / f"{self._safe_name(key)}.json"
        return self._read_json_safe(path)

    def list_state_keys(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "state").glob("*.json"))

    # ------------------------------------------------------------------
    # history/ — append-only JSONL
    # ------------------------------------------------------------------
    def append_history(self, key: str, payload: dict[str, Any]) -> None:
        path = self.root / "history" / f"{self._safe_name(key)}.jsonl"
        with self._lock:
            payload = {**payload, "_ts": now_iso()}
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_history(self, key: str, limit: int | None = None) -> list[dict[str, Any]]:
        path = self.root / "history" / f"{self._safe_name(key)}.jsonl"
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
        if limit is not None and len(lines) > limit:
            lines = lines[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out

    def tail_history(self, key: str, n: int = 5) -> list[dict[str, Any]]:
        return self.read_history(key, limit=n)

    # ------------------------------------------------------------------
    # lessons/   — markdown notes (Reflector writes, humans read)
    # ------------------------------------------------------------------
    def write_lesson(self, title: str, body: str, tags: Iterable[str] = ()) -> Path:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        slug = self._slugify(title)[:60]
        path = self.root / "lessons" / f"{date}-{slug}.md"
        front = "---\n"
        front += f"title: {title}\n"
        front += f"date: {date}\n"
        if tags:
            front += "tags: [" + ", ".join(tags) + "]\n"
        front += "---\n\n"
        with self._lock:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(front + body.strip() + "\n")
        return path

    def list_lessons(self, limit: int = 50) -> list[Path]:
        files = sorted(
            (self.root / "lessons").glob("*.md"),
            key=lambda p: p.name,
            reverse=True,
        )
        return files[:limit]

    # ------------------------------------------------------------------
    # proposals/ — evolver proposals, JSON with status
    # ------------------------------------------------------------------
    def write_proposal(
        self,
        *,
        proposal_id: str,
        summary: str,
        rationale: str,
        diff: dict[str, Any],
        risk: str = "low",
        autodeploy: bool = False,
    ) -> Path:
        path = self.root / "proposals" / f"{self._safe_name(proposal_id)}.json"
        payload = {
            "id": proposal_id,
            "summary": summary,
            "rationale": rationale,
            "diff": diff,
            "risk": risk,
            "autodeploy": autodeploy,
            "status": "pending",
            "created_at": now_iso(),
        }
        self._write_json_atomic(path, payload)
        return path

    def update_proposal_status(self, proposal_id: str, status: str, note: str = "") -> None:
        path = self.root / "proposals" / f"{self._safe_name(proposal_id)}.json"
        data = self._read_json_safe(path)
        data["status"] = status
        data["updated_at"] = now_iso()
        if note:
            data["note"] = note
        self._write_json_atomic(path, data)

    def list_proposals(self, status: str | None = None) -> list[dict[str, Any]]:
        out = []
        for p in sorted((self.root / "proposals").glob("*.json"), reverse=True):
            data = self._read_json_safe(p)
            if data and (status is None or data.get("status") == status):
                data["_path"] = str(p)
                out.append(data)
        return out

    # ------------------------------------------------------------------
    # health/   — healer reports
    # ------------------------------------------------------------------
    def write_health(self, payload: dict[str, Any]) -> None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.root / "health" / f"{date}.jsonl"
        with self._lock:
            payload = {**payload, "_ts": now_iso()}
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_health_today(self) -> list[dict[str, Any]]:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._read_jsonl(self.root / "health" / f"{date}.jsonl")

    # ------------------------------------------------------------------
    # journal/  — agent decisions log (markdown append-only)
    # ------------------------------------------------------------------
    def append_journal(self, agent: str, text: str) -> Path:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = self.root / "journal" / f"{date}.md"
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with self._lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"## {stamp} {agent}\n\n{text.strip()}\n\n")
        return path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)

    @staticmethod
    def _read_json_safe(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        out = []
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
        return out

    @staticmethod
    def _slugify(s: str) -> str:
        out = []
        prev_dash = False
        for ch in s.lower():
            if ch.isalnum():
                out.append(ch)
                prev_dash = False
            elif not prev_dash:
                out.append("-")
                prev_dash = True
        return "".join(out).strip("-")
