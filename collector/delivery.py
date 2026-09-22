from __future__ import annotations

import json
import shutil
from pathlib import Path

from .utils import atomic_write_text, json_dump, now_iso, safe_name, stable_hash, sha256_file


class DeliveryExporter:
    """Build a clean, portable corpus for downstream processing.

    Runtime SQLite files, checkpoints and debug logs are intentionally excluded.
    Only canonical documents whose collector status is success are delivered.
    """

    SCHEMA_VERSION = 'wenshu-delivery-v1'

    def __init__(self, root: Path, state):
        self.root = Path(root)
        self.state = state

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _source_path(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else self.root / path

    def _resolve_original(self, row) -> tuple[Path | None, str]:
        """Resolve historical/stale file_path values without guessing across cases."""
        declared = self._source_path(row['file_path'])
        if declared is not None and declared.is_file():
            return declared, 'file_path'

        fulltext = self._source_path(row['fulltext_path'])
        if fulltext is not None:
            sibling = fulltext.parent / 'official_original.doc'
            if sibling.is_file():
                return sibling, 'fulltext_sibling'

        decision_date = str(row['decision_date'] or '')
        year = decision_date[:4] if len(decision_date) >= 4 else ''
        court = safe_name(row['court'] or 'unknown')
        case_no = safe_name(row['case_no'] or 'unknown')
        if year:
            case_root = self.root / '01_案例原文' / year / court / case_no
            if case_root.is_dir():
                candidates = sorted(case_root.glob('*/official_original.doc'))
                expected_sha = str(row['sha256'] or '').strip()
                if expected_sha:
                    matching = []
                    for candidate in candidates:
                        try:
                            if sha256_file(candidate) == expected_sha:
                                matching.append(candidate)
                        except OSError:
                            continue
                    if len(matching) == 1:
                        return matching[0], 'case_dir_sha256'
                if len(candidates) == 1:
                    return candidates[0], 'case_dir_unique'

        return None, 'missing'

    @staticmethod
    def _delivery_dir_name(source_doc_id: str) -> str:
        prefix = safe_name(source_doc_id, 80)
        suffix = stable_hash(source_doc_id)[:10]
        return f'{prefix}__{suffix}'

    def build(self) -> dict:
        final_root = self.root / '04_数据交付'
        staging_root = self.root / '04_数据交付.__tmp__'

        if staging_root.exists():
            shutil.rmtree(staging_root)
        documents_root = staging_root / 'documents'
        documents_root.mkdir(parents=True, exist_ok=True)

        rows: list[dict] = []
        missing_files: list[dict] = []
        text_count = 0
        stats = self.state.stats()
        success_rows = [
            row for row in self.state.iter_documents()
            if row['status'] == 'success'
        ]

        try:
            for row in success_rows:
                source_doc_id = str(row['source_doc_id'])
                original_src, original_resolution = self._resolve_original(row)
                if original_src is None:
                    missing = {
                        'document_id': source_doc_id,
                        'case_no': row['case_no'],
                        'court': row['court'],
                        'decision_date': row['decision_date'],
                        'file_path': row['file_path'],
                        'fulltext_path': row['fulltext_path'],
                        'sha256': row['sha256'],
                        'reason': 'collector status=success but original DOC is missing',
                    }
                    missing_files.append(missing)
                    print(
                        f"[delivery-skip] success记录缺少原始DOC，已跳过: "
                        f"{row['case_no'] or source_doc_id}"
                    )
                    continue

                source_dir = original_src.parent
                source_document = self._read_json(source_dir / 'document.json')
                source_provenance = self._read_json(source_dir / 'source_provenance.json')
                try:
                    raw = json.loads(row['raw_json'] or '{}')
                    if not isinstance(raw, dict):
                        raw = {}
                except json.JSONDecodeError:
                    raw = {}

                topics = self.state.tags_for(source_doc_id)
                query_sources = self.state.query_sources_for(source_doc_id)
                parties = (
                    source_document.get('parties')
                    or raw.get('17')
                    or raw.get('53')
                )

                delivery_dir = documents_root / self._delivery_dir_name(source_doc_id)
                delivery_dir.mkdir(parents=True, exist_ok=True)

                original_dst = delivery_dir / 'original.doc'
                shutil.copy2(original_src, original_dst)

                fulltext_src = self._source_path(row['fulltext_path'])
                fulltext_dst = None
                if fulltext_src is not None and fulltext_src.is_file():
                    fulltext_dst = delivery_dir / 'fulltext.txt'
                    shutil.copy2(fulltext_src, fulltext_dst)
                    text_count += 1

                rel_dir = delivery_dir.relative_to(staging_root)
                original_rel = (rel_dir / 'original.doc').as_posix()
                fulltext_rel = (
                    (rel_dir / 'fulltext.txt').as_posix()
                    if fulltext_dst is not None else None
                )
                metadata_rel = (rel_dir / 'metadata.json').as_posix()

                index_row = {
                    'document_id': source_doc_id,
                    'case_no': row['case_no'],
                    'title': row['title'],
                    'court': row['court'],
                    'decision_date': row['decision_date'],
                    'doc_type': row['doc_type'],
                    'parties': parties,
                    'topics': topics,
                    'query_sources': query_sources,
                    'source': source_document.get('source') or '中国裁判文书网',
                    'source_code': source_document.get('source_code') or 'C1',
                    'sha256': row['sha256'],
                    'original_path': original_rel,
                    'fulltext_path': fulltext_rel,
                    'metadata_path': metadata_rel,
                    'text_available': fulltext_rel is not None,
                    'parse_error': source_document.get('parse_error'),
                    'original_resolution': original_resolution,
                }

                metadata = {
                    **index_row,
                    'status': 'success',
                    'collected_at': source_document.get('collected_at'),
                    'first_seen_at': row['first_seen_at'],
                    'last_seen_at': row['last_seen_at'],
                    'updated_at': row['updated_at'],
                    'source_provenance': source_provenance,
                    'source_record': raw,
                }
                json_dump(delivery_dir / 'metadata.json', metadata)
                rows.append(index_row)

            atomic_write_text(
                staging_root / 'documents.jsonl',
                ''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in rows),
            )
            atomic_write_text(
                staging_root / 'missing_files.jsonl',
                ''.join(
                    json.dumps(x, ensure_ascii=False) + '\n'
                    for x in missing_files
                ),
            )
            manifest = {
                'schema_version': self.SCHEMA_VERSION,
                'generated_at': now_iso(),
                'source_success_count': len(success_rows),
                'document_count': len(rows),
                'skipped_missing_original_count': len(missing_files),
                'text_document_count': text_count,
                'documents_without_fulltext': len(rows) - text_count,
                'source_status_counts': stats,
                'included_statuses': ['success'],
                'excluded_statuses': sorted(
                    status for status in stats if status != 'success'
                ),
                'entrypoint': 'documents.jsonl',
                'missing_file_report': 'missing_files.jsonl',
                'document_layout': {
                    'original': 'documents/<document_id>/original.doc',
                    'fulltext': 'documents/<document_id>/fulltext.txt (when available)',
                    'metadata': 'documents/<document_id>/metadata.json',
                },
            }
            json_dump(staging_root / 'manifest.json', manifest)

            if final_root.exists():
                shutil.rmtree(final_root)
            staging_root.replace(final_root)
            return manifest
        except Exception:
            if staging_root.exists():
                shutil.rmtree(staging_root)
            raise
