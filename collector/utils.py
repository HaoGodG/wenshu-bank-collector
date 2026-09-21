from __future__ import annotations
import hashlib, json, re
from pathlib import Path
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_hash(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def safe_name(s: str, max_len: int = 120) -> str:
    s = (s or "unknown").strip()
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:max_len] or "unknown")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def json_dump(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))
