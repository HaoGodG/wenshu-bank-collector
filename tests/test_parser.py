from pathlib import Path
from collector.parser import parse_official_doc

def test_parse_captured_official_doc():
    p=Path(__file__).parent/'fixtures'/'sample_official.doc'
    html,text=parse_official_doc(p)
    assert '最高人民法院' in text
    assert '（2026）最高法民申257号' in text
    assert len(text)>1000
    assert '<html' in html.lower()
