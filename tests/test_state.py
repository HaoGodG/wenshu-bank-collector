from collector.state import StateDB

def test_source_doc_dedup_and_query_sources(tmp_path):
    db=StateDB(tmp_path/'s.sqlite3')
    meta={'title':'t','court':'c','case_no':'n','decision_date':'2026-01-01','doc_type':'x'}
    db.upsert_seen('doc1',meta,{'rowkey':'doc1'},['S03'],query_source='金融借款合同纠纷')
    db.upsert_seen('doc1',meta,{'rowkey':'doc1'},['S04'],query_source='银行抵押合同纠纷')
    rows=list(db.iter_documents())
    assert len(rows)==1
    assert db.tags_for('doc1')==['S03','S04']
    assert db.query_sources_for('doc1')==['金融借款合同纠纷','银行抵押合同纠纷']
    db.close()
