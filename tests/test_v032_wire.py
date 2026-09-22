import json
import tempfile
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
        self.native_calls = []
        self.date_context_conditions = []

    async def _ensure_date_page_context(self, conditions):
        self.date_context_conditions = conditions
        raw = self._date_context_value(conditions)
        start, end = raw.split(' TO ', 1)
        self._date_context_data = {
            'queryParams': {'queryItemList': [
                {'id': 's31', 'value': start, 'oper': 'GREATER'},
                {'id': 's31', 'value': end, 'oper': 'LESS'},
                {'id': 's17', 'value': '银行某', 'oper': 'EQUAL'},
            ]},
            'queryResult': {'resultCount': 1, 'resultList': []},
        }

    async def _native_query_current_date_context(self, page_num, page_size, sort_fields):
        self.native_calls.append((page_num, page_size, sort_fields))
        raw = self._date_context_value(self.date_context_conditions)
        start, end = raw.split(' TO ', 1)
        return {
            'queryParams': {'queryItemList': [
                {'id': 's31', 'value': start, 'oper': 'GREATER'},
                {'id': 's31', 'value': end, 'oper': 'LESS'},
                {'id': 's17', 'value': '银行某', 'oper': 'EQUAL'},
            ]},
            'queryResult': {'resultCount': 1, 'resultList': []},
        }

    async def _site_get_data(self, cfg, param):
        self.captured.append((cfg, dict(param)))
        if cfg.endswith('@leftDataItem'):
            return {'s42': {'2026': 10}}
        return {
            'queryParams': {'queryItemList': []},
            'queryResult': {'resultCount': 0, 'resultList': []},
        }


class V033Tests(unittest.IsolatedAsyncioTestCase):
    async def test_query_s17_double_send(self):
        b = CaptureBrowser()
        c = [{'key': 's17', 'value': '银行'}]
        await b.query(c, 1, 15, 's51:desc')
        cfg, p = b.captured[-1]
        self.assertTrue(cfg.endswith('@queryDoc'))
        self.assertEqual(p['s17'], '银行')
        self.assertEqual(json.loads(p['queryCondition']), c)

    async def test_date_query_uses_native_load_data_path(self):
        b = CaptureBrowser()
        c = [
            {'key': 's17', 'value': '银行'},
            {'key': 'cprq', 'value': '2026-03-17 TO 2026-03-31'},
        ]
        data = await b.query(c, 1, 5, 's51:desc')
        self.assertEqual(b.date_context_conditions, c)
        self.assertEqual(b.native_calls, [(1, 5, 's51:desc')])
        self.assertEqual(b.captured, [])
        self.assertTrue(
            WenshuBrowser._response_has_date(
                data, '2026-03-17 TO 2026-03-31'
            )
        )

    async def test_date_query_forces_har_proven_page_size_5(self):
        b = CaptureBrowser()
        b.debug_log_path = None
        b.debug_run_id = 'test'
        c = [
            {'key': 's17', 'value': '银行'},
            {'key': 'cprq', 'value': '2026-03-17 TO 2026-03-31'},
        ]
        await b.query(c, 1, 15, 's51:desc')
        self.assertEqual(b.native_calls, [(1, 5, 's51:desc')])


    async def test_date_query_rebuilds_context_when_native_module_disappears(self):
        class RecoverBrowser(CaptureBrowser):
            def __init__(self):
                super().__init__()
                self.ensure_calls = 0
                self.native_attempts = 0

            async def _ensure_date_page_context(self, conditions):
                self.ensure_calls += 1
                await super()._ensure_date_page_context(conditions)

            async def _native_query_current_date_context(self, page_num, page_size, sort_fields):
                self.native_attempts += 1
                if self.native_attempts == 1:
                    raise RuntimeError(
                        '网页原生日期查询启动失败: loadData1545184311000 missing'
                    )
                return await super()._native_query_current_date_context(
                    page_num, page_size, sort_fields
                )

            async def _page_diag(self):
                return {'url': 'https://wenshu.court.gov.cn/website/wenshu/test'}

            def invalidate_date_context(self, value=None):
                self._date_context_key = None
                self._date_native_only_key = None
                self._date_context_data = None

        b = RecoverBrowser()
        b.debug_log_path = None
        b.debug_run_id = 'test'
        c = [
            {'key': 's17', 'value': '银行'},
            {'key': 'cprq', 'value': '2026-03-17 TO 2026-03-31'},
        ]
        data = await b.query(c, 7, 5, 's51:desc')
        self.assertEqual(b.native_attempts, 2)
        self.assertEqual(b.ensure_calls, 2)
        self.assertTrue(
            WenshuBrowser._response_has_date(
                data, '2026-03-17 TO 2026-03-31'
            )
        )

    async def test_facet_s17_double_send(self):
        b = CaptureBrowser()
        c = [{'key': 's17', 'value': '银行'}]
        await b.facet(c, 's42')
        _, p = b.captured[-1]
        self.assertEqual(p['s17'], '银行')
        self.assertEqual(json.loads(p['queryCondition']), c)

    def test_response_has_date(self):
        data = {
            'queryParams': {
                'queryItemList': [
                    {'id': 's31', 'value': '2026\\-03\\-17', 'oper': 'GREATER'},
                    {'id': 's31', 'value': '2026\\-03\\-31', 'oper': 'LESS'},
                    {'id': 's17', 'value': '银行某', 'oper': 'EQUAL'},
                ]
            }
        }
        self.assertTrue(
            WenshuBrowser._response_has_date(
                data, '2026-03-17 TO 2026-03-31'
            )
        )
        self.assertFalse(
            WenshuBrowser._response_has_date(
                {'queryParams': {'queryItemList': [
                    {'id': 's17', 'value': '银行某', 'oper': 'EQUAL'}
                ]}},
                '2026-03-17 TO 2026-03-31',
            )
        )

    def test_login_mode_supports_auto_manual_and_legacy_flag(self):
        b = WenshuBrowser.__new__(WenshuBrowser)
        b.cfg = {'login_mode': 'manual'}
        self.assertEqual(b._login_mode(), 'manual')
        b.cfg = {'login_mode': 'auto'}
        self.assertEqual(b._login_mode(), 'auto')
        b.cfg = {'auto_login': False}
        self.assertEqual(b._login_mode(), 'manual')
        b.cfg = {'auto_login': True}
        self.assertEqual(b._login_mode(), 'auto')
        b.cfg = {'login_mode': 'invalid'}
        with self.assertRaises(ValueError):
            b._login_mode()

    def test_download_href_encodes_doc_id_without_navigating_url_shape(self):
        href = WenshuBrowser._download_href('（2024）苏1183民初314号 / A+B')
        self.assertTrue(href.startswith('/down/one?docId='))
        self.assertNotIn(' ', href)
        self.assertNotIn('/', href[len('/down/one?docId='):])
        self.assertIn('%EF%BC%882024%EF%BC%89', href)

    def test_query_debug_masks_ciphertext(self):
        form = WenshuBrowser._safe_form(
            'cfg=com.lawyee.judge.dc.parse.dto.SearchDataDsoDTO%40queryDoc'
            '&ciphertext=SECRET123'
            '&s17=%E9%93%B6%E8%A1%8C'
            '&cprqStart=2026-03-17'
            '&cprqEnd=2026-03-31'
        )
        self.assertEqual(form['s17'], '银行')
        self.assertEqual(form['cprqStart'], '2026-03-17')
        self.assertEqual(form['cprqEnd'], '2026-03-31')
        self.assertEqual(form['ciphertext']['length'], len('SECRET123'))
        self.assertEqual(len(form['ciphertext']['sha256']), 64)

    def test_query_debug_record_has_run_id_and_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'query_debug.jsonl'
            b = WenshuBrowser.__new__(WenshuBrowser)
            b.debug_log_path = path
            b.debug_run_id = 'run-1'
            b._debug_append('probe', {'ok': True})
            row = json.loads(path.read_text(encoding='utf-8').strip())
            self.assertEqual(row['run_id'], 'run-1')
            self.assertEqual(row['event_type'], 'probe')
            self.assertTrue(row['recorded_at'])
            self.assertEqual(row['payload'], {'ok': True})

    def test_runner_defaults_match_successful_manual_sort_har(self):
        r = CollectorRunner(
            {},
            Path('/tmp'),
            object(),
            object(),
        )
        self.assertEqual(r.sort, 's51:desc')
        self.assertEqual(r.page_size, 5)

    def test_site_limit_is_hard_capped_600(self):
        r = CollectorRunner(
            {'site_visible_limit': 9999, 'page_size': 15},
            Path('/tmp'),
            object(),
            object(),
        )
        self.assertEqual(r.site_limit, 600)
        self.assertEqual(r.max_pages, 40)

    def test_s17_backend_acceptance(self):
        req = [Condition('s17', '银行')]
        data = {
            'queryParams': {
                'queryItemList': [
                    {'id': 's17', 'value': '银行', 'oper': 'EQUAL'}
                ]
            }
        }
        ok, missing, _ = CollectorRunner.check_backend_conditions(req, data)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_s17_backend_anonymised_value_is_accepted(self):
        req = [Condition('s17', '银行')]
        data = {
            'queryParams': {
                'queryItemList': [
                    {'id': 's17', 'value': '银行某', 'oper': 'EQUAL'}
                ]
            }
        }
        ok, missing, accepted = CollectorRunner.check_backend_conditions(req, data)
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertEqual(accepted[0]['value'], '银行某')

    def test_date_backend_translation_is_accepted(self):
        req = [
            Condition('s17', '银行'),
            Condition('cprq', '2026-03-17 TO 2026-03-31'),
        ]
        data = {
            'queryParams': {
                'queryItemList': [
                    {'id': 's31', 'value': '2026\\-03\\-17', 'oper': 'GREATER'},
                    {'id': 's31', 'value': '2026\\-03\\-31', 'oper': 'LESS'},
                    {'id': 's17', 'value': '银行某', 'oper': 'EQUAL'},
                ]
            }
        }
        ok, missing, _ = CollectorRunner.check_backend_conditions(req, data)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_s17_unrelated_backend_value_is_rejected(self):
        req = [Condition('s17', '银行')]
        data = {
            'queryParams': {
                'queryItemList': [
                    {'id': 's17', 'value': '保险公司', 'oper': 'EQUAL'}
                ]
            }
        }
        ok, missing, _ = CollectorRunner.check_backend_conditions(req, data)
        self.assertFalse(ok)
        self.assertEqual(missing, ['s17=银行'])


if __name__ == '__main__':
    unittest.main()
