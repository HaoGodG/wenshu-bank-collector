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

    async def test_date_query_keeps_historic_direct_querydoc_path(self):
        b = CaptureBrowser()
        c = [
            {'key': 's17', 'value': '银行'},
            {'key': 'cprq', 'value': '2026-03-17 TO 2026-03-31'},
        ]
        await b.query(c, 1, 15, 's51:desc')
        cfg, p = b.captured[-1]
        self.assertTrue(cfg.endswith('@queryDoc'))
        self.assertEqual(p['s17'], '银行')
        self.assertEqual(p['cprqStart'], '2026-03-17')
        self.assertEqual(p['cprqEnd'], '2026-03-31')
        self.assertEqual(p['sortFields'], 's51:desc')
        self.assertEqual(p['pageNum'], 1)
        self.assertEqual(p['pageSize'], 15)
        self.assertEqual(json.loads(p['queryCondition']), c)

    async def test_facet_s17_double_send(self):
        b = CaptureBrowser()
        c = [{'key': 's17', 'value': '银行'}]
        await b.facet(c, 's42')
        _, p = b.captured[-1]
        self.assertEqual(p['s17'], '银行')
        self.assertEqual(json.loads(p['queryCondition']), c)

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
