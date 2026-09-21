from __future__ import annotations
import json, sqlite3
from pathlib import Path
from .utils import now_iso, stable_hash

SCHEMA = r'''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS documents (
  source_doc_id TEXT PRIMARY KEY,
  title TEXT, court TEXT, case_no TEXT, decision_date TEXT, doc_type TEXT,
  raw_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'seen',
  file_path TEXT, fulltext_path TEXT, html_path TEXT,
  sha256 TEXT, duplicate_of TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS ix_documents_sha256 ON documents(sha256);
CREATE INDEX IF NOT EXISTS ix_documents_case ON documents(case_no, court, decision_date);
CREATE TABLE IF NOT EXISTS document_tags (
  source_doc_id TEXT NOT NULL,
  tag TEXT NOT NULL,
  PRIMARY KEY(source_doc_id, tag)
);
CREATE TABLE IF NOT EXISTS document_queries (
  source_doc_id TEXT NOT NULL,
  seed_name TEXT NOT NULL,
  PRIMARY KEY(source_doc_id, seed_name)
);
CREATE TABLE IF NOT EXISTS slices (
  slice_key TEXT PRIMARY KEY,
  seed_name TEXT NOT NULL,
  conditions_json TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  result_count INTEGER,
  status TEXT NOT NULL,
  note TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_time TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
'''

class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    def slice_key(self, seed_name, conditions, start_date, end_date):
        vals = sorted((c.key, c.value) for c in conditions)
        return stable_hash([seed_name, vals, str(start_date), str(end_date)])[:32]

    def get_slice_status(self, key: str):
        row = self.db.execute("SELECT status FROM slices WHERE slice_key=?", (key,)).fetchone()
        return row[0] if row else None

    def save_slice(self, key, seed_name, conditions, start_date, end_date, status, result_count=None, note=None):
        now = now_iso()
        cond = json.dumps([{"key": c.key, "value": c.value} for c in conditions], ensure_ascii=False, sort_keys=True)
        self.db.execute('''
          INSERT INTO slices(slice_key,seed_name,conditions_json,start_date,end_date,result_count,status,note,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?)
          ON CONFLICT(slice_key) DO UPDATE SET result_count=excluded.result_count,status=excluded.status,note=excluded.note,updated_at=excluded.updated_at
        ''', (key, seed_name, cond, str(start_date), str(end_date), result_count, status, note, now))
        self.db.commit()

    def upsert_seen(self, source_doc_id: str, meta: dict, raw: dict, tags: list[str], query_source: str | None = None):
        now = now_iso()
        self.db.execute('''
          INSERT INTO documents(source_doc_id,title,court,case_no,decision_date,doc_type,raw_json,first_seen_at,last_seen_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(source_doc_id) DO UPDATE SET title=excluded.title,court=excluded.court,case_no=excluded.case_no,
            decision_date=excluded.decision_date,doc_type=excluded.doc_type,raw_json=excluded.raw_json,last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at
        ''', (source_doc_id, meta.get('title'), meta.get('court'), meta.get('case_no'), meta.get('decision_date'), meta.get('doc_type'),
              json.dumps(raw, ensure_ascii=False), now, now, now))
        for tag in tags:
            self.db.execute("INSERT OR IGNORE INTO document_tags(source_doc_id,tag) VALUES(?,?)", (source_doc_id, tag))
        if query_source:
            self.db.execute("INSERT OR IGNORE INTO document_queries(source_doc_id,seed_name) VALUES(?,?)", (source_doc_id, query_source))
        self.db.commit()

    def should_download(self, source_doc_id: str) -> bool:
        row = self.db.execute("SELECT status FROM documents WHERE source_doc_id=?", (source_doc_id,)).fetchone()
        return not row or row[0] not in ('success','duplicate','restricted')

    def mark_attempt(self, source_doc_id: str):
        self.db.execute("UPDATE documents SET attempts=attempts+1,updated_at=? WHERE source_doc_id=?", (now_iso(), source_doc_id))
        self.db.commit()

    def find_by_sha(self, sha: str):
        return self.db.execute("SELECT source_doc_id,file_path FROM documents WHERE sha256=? AND status='success' LIMIT 1", (sha,)).fetchone()

    def mark_success(self, source_doc_id, file_path, fulltext_path, html_path, sha):
        self.db.execute("UPDATE documents SET status='success',file_path=?,fulltext_path=?,html_path=?,sha256=?,last_error=NULL,updated_at=? WHERE source_doc_id=?",
                        (file_path, fulltext_path, html_path, sha, now_iso(), source_doc_id))
        self.db.commit()

    def mark_duplicate(self, source_doc_id, sha, duplicate_of):
        self.db.execute("UPDATE documents SET status='duplicate',sha256=?,duplicate_of=?,last_error=NULL,updated_at=? WHERE source_doc_id=?",
                        (sha, duplicate_of, now_iso(), source_doc_id))
        self.db.commit()

    def mark_failure(self, source_doc_id, status, error):
        self.db.execute("UPDATE documents SET status=?,last_error=?,updated_at=? WHERE source_doc_id=?", (status, error, now_iso(), source_doc_id))
        self.db.commit()

    def event(self, event_type: str, payload: dict):
        self.db.execute("INSERT INTO events(event_time,event_type,payload_json) VALUES(?,?,?)", (now_iso(), event_type, json.dumps(payload, ensure_ascii=False)))
        self.db.commit()

    def latest_success_decision_date(self):
        """Newest decision date present in the local successfully-downloaded corpus.

        This is intentionally based on local download records, not remote slice
        checkpoints.  The collector uses it as the inclusive upper bound when
        backfilling older cases in descending date order.
        """
        row = self.db.execute(
            "SELECT MAX(decision_date) FROM documents "
            "WHERE status='success' AND decision_date IS NOT NULL AND length(decision_date)>=10"
        ).fetchone()
        return row[0] if row and row[0] else None

    def stats(self):
        rows = self.db.execute("SELECT status,COUNT(*) c FROM documents GROUP BY status").fetchall()
        return {r['status']: r['c'] for r in rows}

    def iter_documents(self):
        yield from self.db.execute("SELECT * FROM documents ORDER BY first_seen_at,source_doc_id")

    def tags_for(self, source_doc_id):
        return [r[0] for r in self.db.execute("SELECT tag FROM document_tags WHERE source_doc_id=? ORDER BY tag", (source_doc_id,))]

    def query_sources_for(self, source_doc_id):
        return [r[0] for r in self.db.execute("SELECT seed_name FROM document_queries WHERE source_doc_id=? ORDER BY seed_name", (source_doc_id,))]
