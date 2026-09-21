import json
import unittest
from pathlib import Path

from collector.models import Condition
from collector.runner import CollectorRunner
from collector.site_client import WenshuBrowser


class FakePage:
    async def evaluate(self, expression, *args):
        if expression == 'cipher()':
            return 'TEST_CIPHER'
        raise AssertionError(expression)


class CaptureBrowser(WenshuBrowser):
    def __init__(self):
        self.cfg = {'query_wait_timeout_seconds': 5}
        self.page = FakePage()
        self.captured = []

    async def _site_get_data(self, cfg, param):
        self.captured.append((cfg, dict(param)))
        if cfg.endswith('@leftDataItem'):
            return {'s42': {'2026': 10}}
        return {'queryParams': {'queryItemList': []}, 'queryResult': {'resultCount': 0, 'resultList': []}}


class V033Tests(unittest.IsolatedAsyncioTestCase):
    async def test_query_s17_double_send(self):
        b = CaptureBrowser()
        c = [{'key': 's17', 'value': '银行'}]
        await b.query(c, 1, 15, 's51:desc')
        _, p = b.captured[-1]
        self.assertEqual(p['s17'], '银行')
        self.assertEqual(json.loads(p['queryCondition']), c)

    async def test_facet_s17_double_send(self):
        b = CaptureBrowser()
        c = [{'key': 's17', 'value': '银行'}]
        await b.facet(c, 's42')
        _, p = b.captured[-1]
        self.assertEqual(p['s17'], '银行')
        self.assertEqual(json.loads(p['queryCondition']), c)

    async def test_date_query_runs_official_cprq_preflight_once(self):
        class DatePage:
            def __init__(self):
                self.preflights = []
            async def evaluate(self, expression, *args):
                if expression == 'cipher()':
                    return 'TEST_CIPHER'
                if '/api/fp/cprq' in expression:
                    self.preflights.append(args[0])
                    return {'ok': True, 'status': 200}
                raise AssertionError(expression)

        b = CaptureBrowser()
        b.page = DatePage()
        conditions = [
            {'key': 's17', 'value': '银行'},
            {'key': 'cprq', 'value': '2026-03-16 TO 2026-04-01'},
        ]
        await b.query(conditions, 1, 15, 's51:desc')
        await b.query(conditions, 2, 15, 's51:desc')
        self.assertEqual(b.page.preflights, [
            {'startDate': '2026-03-16', 'endDate': '2026-04-01'}
        ])

        b.invalidate_prepared_date('2026-03-16 TO 2026-04-01')
        await b.query(conditions, 3, 15, 's51:desc')
        self.assertEqual(len(b.page.preflights), 2)

    async def test_date_preflight_matches_har_submit_shape(self):
        class DatePage:
            async def evaluate(self, expression, payload):
                self.asserted_expression = expression
                self.asserted_payload = payload
                return {'ok': True, 'status': 200}

        b = CaptureBrowser()
        b.page = DatePage()
        await b._prepare_date_filter([
            {'key': 's17', 'value': '银行'},
            {'key': 'cprq', 'value': '2026-03-16 TO 2026-04-01'},
        ])
        self.assertIn("fetch('/api/fp/cprq'", b.page.asserted_expression)
        self.assertIn("gjjsSubmit: '1'", b.page.asserted_expression)
        self.assertIn("'X-Requested-With': 'XMLHttpRequest'", b.page.asserted_expression)
        self.assertEqual(
            b.page.asserted_payload,
            {'startDate': '2026-03-16', 'endDate': '2026-04-01'}
        )

    async def test_site_get_data_disables_url_param_merge(self):
        class CapturePage:
            url = 'https://wenshu.court.gov.cn/website/wenshu/181217BMTKHNT2W0/index.html?pageId=test'
            def __init__(self):
                self.expression = ''
                self.payload = None
            async def evaluate(self, expression, payload):
                self.expression = expression
                self.payload = payload
                return {'ok': True}

        b = CaptureBrowser()
        page = CapturePage()
        b.page = page
        result = await b._site_get_data('cfg@test', {'queryCondition': '[]'})
        self.assertEqual(result, {'ok': True})
        self.assertIn('readUrlParam: false', page.expression)
        self.assertIn('getParameter("pageId")', page.expression)
        self.assertEqual(page.payload['param']['queryCondition'], '[]')

    async def test_query_checked_rearms_date_after_backend_drops_it(self):
        class RetryBrowser:
            def __init__(self):
                self.calls = 0
                self.invalidated = []
            async def query(self, conditions, page_num, page_size, sort_fields):
                self.calls += 1
                if self.calls == 1:
                    return {'queryParams': {'queryItemList': [
                        {'id': 's17', 'value': '银行某', 'oper': 'EQUAL'}
                    ]}}
                return {'queryParams': {'queryItemList': [
                    {'id': 's17', 'value': '银行某', 'oper': 'EQUAL'},
                    {'id': 's31', 'value': '2026-03-16', 'oper': 'GREATER'},
                    {'id': 's31', 'value': '2026-04-01', 'oper': 'LESS'},
                ]}}
            def invalidate_prepared_date(self, value=None):
                self.invalidated.append(value)

        b = RetryBrowser()
        r = CollectorRunner({
            'page_size': 15,
            'request_interval_seconds': 0,
            'query_condition_verify_retries': 2,
        }, Path('/tmp'), object(), b)
        conditions = [
            Condition('s17', '银行'),
            Condition('cprq', '2026-03-16 TO 2026-04-01'),
        ]
        await r.query_checked(conditions, 1)
        self.assertEqual(b.calls, 2)
        self.assertEqual(b.invalidated, ['2026-03-16 TO 2026-04-01'])

    def test_site_limit_is_hard_capped_600(self):
        r = CollectorRunner({'site_visible_limit': 9999, 'page_size': 15}, Path('/tmp'), object(), object())
        self.assertEqual(r.site_limit, 600)
        self.assertEqual(r.max_pages, 40)

    def test_s17_backend_acceptance(self):
        req = [Condition('s17', '银行')]
        data = {'queryParams': {'queryItemList': [{'id': 's17', 'value': '银行', 'oper': 'EQUAL'}]}}
        ok, missing, _ = CollectorRunner.check_backend_conditions(req, data)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_s17_backend_anonymised_value_is_accepted(self):
        # Real bank-core.har: request s17=银行; backend queryItemList value=银行某.
        req = [Condition('s17', '银行')]
        data = {'queryParams': {'queryItemList': [{'id': 's17', 'value': '银行某', 'oper': 'EQUAL'}]}}
        ok, missing, accepted = CollectorRunner.check_backend_conditions(req, data)
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertEqual(accepted[0]['value'], '银行某')

    def test_s17_unrelated_backend_value_is_rejected(self):
        req = [Condition('s17', '银行')]
        data = {'queryParams': {'queryItemList': [{'id': 's17', 'value': '保险公司', 'oper': 'EQUAL'}]}}
        ok, missing, _ = CollectorRunner.check_backend_conditions(req, data)
        self.assertFalse(ok)
        self.assertEqual(missing, ['s17=银行'])


if __name__ == '__main__':
    unittest.main()
