from pathlib import Path
from collector.models import Condition
from collector.runner import CollectorRunner

class DummyState: pass
class DummyBrowser: pass

def make_runner(limit=600,page_size=15):
    return CollectorRunner({
        'page_size':page_size,
        'max_results_per_seed':limit,
        'request_interval_seconds':0,
        'sort_fields':'s51:desc',
        'query_condition_verify_retries':3,
    },Path('/tmp'),DummyState(),DummyBrowser())

def test_visible_cap_is_40_pages():
    r=make_runner(600,15)
    assert r.max_pages==40
    assert r.sort=='s51:desc'

def test_backend_condition_verification_repeated_s21():
    req=[Condition('s21','银行'),Condition('s21','征信')]
    data={'queryParams':{'queryItemList':[
        {'id':'s21','value':'银行','oper':'EQUAL'},
        {'id':'s21','value':'征信','oper':'EQUAL'},
    ]}}
    ok,missing,_=CollectorRunner.check_backend_conditions(req,data)
    assert ok and not missing

def test_backend_condition_verification_exact_cause():
    req=[Condition('s15','9178')]
    data={'queryParams':{'queryItemList':[{'id':'s15','value':'9178','oper':'EQUAL'}]}}
    ok,missing,_=CollectorRunner.check_backend_conditions(req,data)
    assert ok and not missing
