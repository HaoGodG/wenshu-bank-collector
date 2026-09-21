import asyncio
from datetime import date

import pytest
from pathlib import Path

from collector.models import Condition, QuerySeed
from collector.planner import bisect_dates, with_date_condition
from collector.runner import CollectorRunner
from collector.site_client import WenshuBrowser
from collector.state import StateDB


def test_bisect_2026_halves_without_gap():
    left, right = bisect_dates(date(2026, 1, 1), date(2026, 12, 31))
    assert left == (date(2026, 1, 1), date(2026, 6, 30))
    assert right == (date(2026, 7, 1), date(2026, 12, 31))
    assert left[1].toordinal() + 1 == right[0].toordinal()


def test_wire_range_matches_website_inclusive_dates():
    c = with_date_condition([Condition('s17', '银行')], date(2026, 1, 1), date(2026, 6, 30))
    assert c[-1].key == 'cprq'
    assert c[-1].value == '2026-01-01 TO 2026-06-30'


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


def test_cprq_is_mirrored_from_current_condition_not_url():
    p = {'queryCondition': '[]'}
    WenshuBrowser._inject_wire_conditions(p, [
        {'key': 's17', 'value': '银行'},
        {'key': 'cprq', 'value': '2026-03-16 TO 2026-04-01'},
    ])
    assert p['s17'] == '银行'
    assert p['cprqStart'] == '2026-03-16'
    assert p['cprqEnd'] == '2026-04-01'
    assert 'cprq' not in p


def test_probe_defaults_keep_resume_disabled_until_native_s51_is_verified(tmp_path):
    db = StateDB(tmp_path / 'state.sqlite3')
    r = CollectorRunner(
        {'page_size': 5, 'sort_fields': 's51:desc', 'resume_from_local_latest': False},
        tmp_path,
        db,
        object(),
    )
    assert r.sort == 's51:desc'
    assert r.page_size == 5
    assert r.resume_from_local_latest is False
    db.close()


def test_local_latest_success_date(tmp_path):
    db = StateDB(tmp_path / 'state.sqlite3')
    for i, d in enumerate(['2026-06-11', '2026-05-01', '2025-12-31'], 1):
        doc = f'doc{i}'
        meta = {'title': 't', 'court': 'c', 'case_no': str(i), 'decision_date': d, 'doc_type': 'x'}
        db.upsert_seen(doc, meta, {'rowkey': doc}, ['BANK_PARTY'], query_source='银行作为当事人')
        db.mark_success(doc, f'{doc}.doc', None, None, f'sha{i}')
    assert db.latest_success_decision_date() == '2026-06-11'
    db.close()


def test_session_lost_detector_matches_target_closed_message():
    err = RuntimeError('Page.evaluate: Target page, context or browser has been closed')
    assert WenshuBrowser.is_session_lost_error(err)
    assert not WenshuBrowser.is_session_lost_error(RuntimeError('ordinary query failure'))


def test_query_checked_recovers_dead_browser_and_retries_same_page(tmp_path):
    class FakeBrowser:
        def __init__(self):
            self.query_calls = 0
            self.recover_calls = 0

        @staticmethod
        def is_session_lost_error(exc):
            return WenshuBrowser.is_session_lost_error(exc)

        async def recover_session(self, reason=''):
            self.recover_calls += 1

        def invalidate_date_context(self, value=None):
            pass

        async def query(self, requested, page_num, page_size, sort_fields):
            self.query_calls += 1
            if self.query_calls == 1:
                raise RuntimeError('Page.evaluate: Target page, context or browser has been closed')
            return {
                'queryParams': {'queryItemList': [
                    {'id': 's17', 'oper': 'EQUAL', 'value': '银行某'},
                ]},
                'queryResult': {'resultCount': 1, 'resultList': []},
            }

    db = StateDB(tmp_path / 'state.sqlite3')
    browser = FakeBrowser()
    runner = CollectorRunner(
        {
            'page_size': 5,
            'sort_fields': 's51:desc',
            'request_interval_seconds': 0,
            'query_condition_verify_retries': 1,
            'browser_session_recovery_retries': 2,
        },
        tmp_path,
        db,
        browser,
    )
    data = asyncio.run(runner.query_checked([Condition('s17', '银行')], 32))
    assert data['queryResult']['resultCount'] == 1
    assert browser.query_calls == 2
    assert browser.recover_calls == 1
    db.close()


def test_leaf_with_failed_document_is_not_marked_completed(tmp_path):
    db = StateDB(tmp_path / 'state.sqlite3')
    runner = CollectorRunner(
        {'page_size': 5, 'sort_fields': 's51:desc', 'request_interval_seconds': 0},
        tmp_path,
        db,
        object(),
    )
    conditions = [
        Condition('s17', '银行'),
        Condition('cprq', '2025-09-01 TO 2025-09-15'),
    ]
    seed = QuerySeed('银行作为当事人', ['BANK_PARTY'], conditions)
    first = {
        'queryResult': {
            'resultCount': 2,
            'resultList': [
                {'rowkey': 'ok'},
                {'rowkey': 'bad'},
            ],
        }
    }

    async def fake_handle(item, _seed):
        return 'failed' if item.get('rowkey') == 'bad' else 'success'

    runner.handle_result = fake_handle
    with pytest.raises(RuntimeError, match='未标记 completed'):
        asyncio.run(
            runner._process_leaf(
                seed,
                conditions,
                '2025-09-01',
                '2025-09-15',
                first,
                2,
                True,
            )
        )
    key = runner._slice_key(seed, conditions, '2025-09-01', '2025-09-15')
    assert db.get_slice_status(key) == 'failed'
    db.close()
