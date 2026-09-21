from datetime import date
from pathlib import Path

from collector.models import Condition
from collector.planner import bisect_dates, with_date_condition
from collector.runner import CollectorRunner
from collector.site_client import WenshuBrowser
from collector.state import StateDB


def test_bisect_2026_halves_without_gap():
    left, right = bisect_dates(date(2026, 1, 1), date(2026, 12, 31))
    assert left == (date(2026, 1, 1), date(2026, 6, 30))
    assert right == (date(2026, 7, 1), date(2026, 12, 31))
    assert left[1].toordinal() + 1 == right[0].toordinal()


def test_wire_range_expands_inclusive_boundaries():
    c = with_date_condition([Condition('s17', '银行')], date(2026, 1, 1), date(2026, 6, 30))
    assert c[-1].key == 'cprq'
    assert c[-1].value == '2025-12-31 TO 2026-07-01'


def test_har_date_backend_translation_is_accepted():
    req = [Condition('s17', '银行'), Condition('cprq', '2026-09-08 TO 2026-09-15')]
    data = {'queryParams': {'queryItemList': [
        {'id': 's31', 'oper': 'GREATER', 'value': '2026\\-09\\-08', 'not': False},
        {'id': 's31', 'oper': 'LESS', 'value': '2026\\-09\\-15', 'not': False},
        {'id': 's17', 'oper': 'EQUAL', 'value': '银行某', 'not': False},
    ]}}
    ok, missing, _ = CollectorRunner.check_backend_conditions(req, data)
    assert ok
    assert missing == []


def test_cprq_stays_only_in_query_condition():
    p = {'queryCondition': '[]'}
    WenshuBrowser._inject_wire_conditions(p, [
        {'key': 's17', 'value': '银行'},
        {'key': 'cprq', 'value': '2025-12-31 TO 2026-07-01'},
    ])
    assert p['s17'] == '银行'
    assert 'cprqStart' not in p
    assert 'cprqEnd' not in p
    assert 'cprq' not in p


def test_local_latest_success_date(tmp_path):
    db = StateDB(tmp_path / 'state.sqlite3')
    for i, d in enumerate(['2026-06-11', '2026-05-01', '2025-12-31'], 1):
        doc = f'doc{i}'
        meta = {'title': 't', 'court': 'c', 'case_no': str(i), 'decision_date': d, 'doc_type': 'x'}
        db.upsert_seen(doc, meta, {'rowkey': doc}, ['BANK_PARTY'], query_source='银行作为当事人')
        db.mark_success(doc, f'{doc}.doc', None, None, f'sha{i}')
    assert db.latest_success_decision_date() == '2026-06-11'
    db.close()
