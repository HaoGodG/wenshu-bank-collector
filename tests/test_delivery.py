import json

from collector.delivery import DeliveryExporter
from collector.state import StateDB


def _add_document(db, doc_id, status, *, file_path=None, fulltext_path=None, sha=None):
    meta = {
        'title': f'{doc_id}标题',
        'court': '某人民法院',
        'case_no': f'（2026）某民初{doc_id[-1]}号',
        'decision_date': '2026-09-01',
        'doc_type': '判决书',
    }
    raw = {'rowkey': doc_id, '17': '某银行、某公司'}
    db.upsert_seen(
        doc_id,
        meta,
        raw,
        ['BANK_PARTY'],
        query_source='银行作为当事人',
    )
    if status == 'success':
        db.mark_success(
            doc_id,
            file_path,
            fulltext_path,
            None,
            sha or f'sha-{doc_id}',
        )
    elif status == 'duplicate':
        db.mark_duplicate(doc_id, sha or f'sha-{doc_id}', 'doc-success')
    else:
        db.mark_failure(doc_id, status, f'{status} test')


def test_delivery_exports_only_success_documents(tmp_path):
    root = tmp_path / 'data'
    db = StateDB(root / '03_采集运行记录' / 'collector.sqlite3')

    source_dir = (
        root / '01_案例原文' / '2026' / '某人民法院' / 'case' / 'doc-success'
    )
    source_dir.mkdir(parents=True)
    (source_dir / 'official_original.doc').write_bytes(b'official-doc')
    (source_dir / 'official_fulltext.txt').write_text('这是正文', encoding='utf-8')
    (source_dir / 'official_fulltext.html').write_text(
        '<p>这是正文</p>',
        encoding='utf-8',
    )
    (source_dir / 'document.json').write_text(
        json.dumps({
            'parties': '某银行、某公司',
            'source': '中国裁判文书网',
            'source_code': 'C1',
            'parse_error': None,
            'collected_at': '2026-09-22T01:00:00+00:00',
        }, ensure_ascii=False),
        encoding='utf-8',
    )
    (source_dir / 'source_provenance.json').write_text(
        json.dumps({'wenshu_doc_id': 'doc-success'}, ensure_ascii=False),
        encoding='utf-8',
    )

    _add_document(
        db,
        'doc-success',
        'success',
        file_path=str((source_dir / 'official_original.doc').relative_to(root)),
        fulltext_path=str((source_dir / 'official_fulltext.txt').relative_to(root)),
        sha='abc123',
    )
    _add_document(db, 'doc-duplicate', 'duplicate')
    _add_document(db, 'doc-retry', 'retry')

    stale = root / '04_数据交付' / 'stale.txt'
    stale.parent.mkdir(parents=True)
    stale.write_text('old', encoding='utf-8')

    manifest = DeliveryExporter(root, db).build()
    delivery = root / '04_数据交付'

    assert manifest['document_count'] == 1
    assert manifest['text_document_count'] == 1
    assert not stale.exists()
    assert not (delivery / 'collector.sqlite3').exists()
    assert not any(delivery.rglob('*.html'))

    rows = [
        json.loads(line)
        for line in (delivery / 'documents.jsonl').read_text(
            encoding='utf-8'
        ).splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row['document_id'] == 'doc-success'
    assert row['text_available'] is True
    assert row['parties'] == '某银行、某公司'

    assert (delivery / row['original_path']).read_bytes() == b'official-doc'
    assert (delivery / row['fulltext_path']).read_text(encoding='utf-8') == '这是正文'

    metadata = json.loads(
        (delivery / row['metadata_path']).read_text(encoding='utf-8')
    )
    assert metadata['source_record']['rowkey'] == 'doc-success'
    assert metadata['source_provenance']['wenshu_doc_id'] == 'doc-success'
    assert metadata['topics'] == ['BANK_PARTY']
    assert metadata['query_sources'] == ['银行作为当事人']

    db.close()


def test_delivery_keeps_success_without_fulltext(tmp_path):
    root = tmp_path / 'data'
    db = StateDB(root / '03_采集运行记录' / 'collector.sqlite3')

    source_dir = (
        root / '01_案例原文' / '2026' / '某人民法院' / 'case' / 'doc-no-text'
    )
    source_dir.mkdir(parents=True)
    (source_dir / 'official_original.doc').write_bytes(b'official-doc')
    (source_dir / 'document.json').write_text(
        json.dumps({'parse_error': 'parse failed'}, ensure_ascii=False),
        encoding='utf-8',
    )

    _add_document(
        db,
        'doc-no-text',
        'success',
        file_path=str((source_dir / 'official_original.doc').relative_to(root)),
        fulltext_path=None,
        sha='def456',
    )

    manifest = DeliveryExporter(root, db).build()
    row = json.loads(
        (root / '04_数据交付' / 'documents.jsonl').read_text(
            encoding='utf-8'
        ).strip()
    )
    assert manifest['document_count'] == 1
    assert manifest['text_document_count'] == 0
    assert row['fulltext_path'] is None
    assert row['text_available'] is False

    db.close()


def test_delivery_reports_and_skips_success_with_missing_original(tmp_path):
    root = tmp_path / 'data'
    db = StateDB(root / '03_采集运行记录' / 'collector.sqlite3')

    _add_document(
        db,
        'doc-missing',
        'success',
        file_path='01_案例原文/missing/official_original.doc',
        fulltext_path=None,
    )

    manifest = DeliveryExporter(root, db).build()
    delivery = root / '04_数据交付'

    assert manifest['source_success_count'] == 1
    assert manifest['document_count'] == 0
    assert manifest['skipped_missing_original_count'] == 1

    missing = [
        json.loads(line)
        for line in (delivery / 'missing_files.jsonl').read_text(
            encoding='utf-8'
        ).splitlines()
        if line.strip()
    ]
    assert len(missing) == 1
    assert missing[0]['document_id'] == 'doc-missing'
    assert 'original DOC is missing' in missing[0]['reason']
    assert (delivery / 'documents.jsonl').read_text(encoding='utf-8') == ''

    db.close()


def test_delivery_recovers_original_from_fulltext_sibling(tmp_path):
    root = tmp_path / 'data'
    db = StateDB(root / '03_采集运行记录' / 'collector.sqlite3')

    source_dir = (
        root / '01_案例原文' / '2026' / '某人民法院' / 'case' / 'doc-recover'
    )
    source_dir.mkdir(parents=True)
    (source_dir / 'official_original.doc').write_bytes(b'recovered-doc')
    (source_dir / 'official_fulltext.txt').write_text('正文', encoding='utf-8')

    _add_document(
        db,
        'doc-recover',
        'success',
        file_path='01_案例原文/stale/path/official_original.doc',
        fulltext_path=str(
            (source_dir / 'official_fulltext.txt').relative_to(root)
        ),
        sha='recover-sha',
    )

    manifest = DeliveryExporter(root, db).build()
    assert manifest['document_count'] == 1
    assert manifest['skipped_missing_original_count'] == 0

    row = json.loads(
        (root / '04_数据交付' / 'documents.jsonl').read_text(
            encoding='utf-8'
        ).strip()
    )
    assert row['original_resolution'] == 'fulltext_sibling'
    assert (
        root / '04_数据交付' / row['original_path']
    ).read_bytes() == b'recovered-doc'

    db.close()
