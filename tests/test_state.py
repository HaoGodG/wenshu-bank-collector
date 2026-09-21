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


def test_pre_download_duplicate_uses_strong_metadata(tmp_path):
    db=StateDB(tmp_path/'s.sqlite3')
    meta={
        'title':'甲某与<span style="color:red">银行</span>金融借款合同纠纷判决书',
        'court':'某市中级人民法院',
        'case_no':'（2026）某01民终1号',
        'decision_date':'2026-03-31',
        'doc_type':None,
    }
    db.upsert_seen('doc1',meta,{'rowkey':'doc1'},['BANK_PARTY'],query_source='银行作为当事人')
    db.mark_success('doc1','doc1.doc',None,None,'sha1')

    meta2={**meta,'title':'甲某与银行金融借款合同纠纷判决书'}
    db.upsert_seen('doc2',meta2,{'rowkey':'doc2'},['BANK_PARTY'],query_source='银行作为当事人')
    dup=db.find_pre_download_duplicate('doc2',meta2)
    assert dup is not None
    assert dup['canonical_doc_id']=='doc1'

    db.mark_pre_download_duplicate('doc2','doc1','metadata match')
    rows={r['source_doc_id']:r for r in db.iter_documents()}
    assert rows['doc2']['status']=='duplicate'
    assert rows['doc2']['duplicate_of']=='doc1'
    db.close()


def test_pre_download_duplicate_fails_open_when_title_differs(tmp_path):
    db=StateDB(tmp_path/'s.sqlite3')
    base={
        'title':'甲案判决书',
        'court':'某市中级人民法院',
        'case_no':'（2026）某01民终1号',
        'decision_date':'2026-03-31',
        'doc_type':None,
    }
    db.upsert_seen('doc1',base,{'rowkey':'doc1'},['BANK_PARTY'],query_source='银行作为当事人')
    db.mark_success('doc1','doc1.doc',None,None,'sha1')
    other={**base,'title':'甲案裁定书'}
    db.upsert_seen('doc2',other,{'rowkey':'doc2'},['BANK_PARTY'],query_source='银行作为当事人')
    assert db.find_pre_download_duplicate('doc2',other) is None
    db.close()
