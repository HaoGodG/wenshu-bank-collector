from datetime import date
from collector.planner import bisect_dates, with_date_condition
from collector.models import Condition

def test_bisect_dates_no_gap():
    a,b=bisect_dates(date(2026,1,1),date(2026,1,10))
    assert a==(date(2026,1,1),date(2026,1,5))
    assert b==(date(2026,1,6),date(2026,1,10))

def test_date_condition():
    c=with_date_condition([Condition('s15','9178')],date(2026,1,1),date(2026,12,31))
    assert c[-1].key=='cprq' and c[-1].value=='2025-12-31 TO 2027-01-01'
