from __future__ import annotations
import json
from pathlib import Path
from .utils import atomic_write_text, json_dump, now_iso

class Exporter:
    def __init__(self, root: Path, state):
        self.root=root; self.state=state

    def export_all(self):
        idx=self.root/'00_总索引'; run=self.root/'03_采集运行记录'
        idx.mkdir(parents=True,exist_ok=True); run.mkdir(parents=True,exist_ok=True)
        docs=[]; pending=[]; cases={}
        for r in self.state.iter_documents():
            d=dict(r); d['topics']=self.state.tags_for(r['source_doc_id']); d['query_sources']=self.state.query_sources_for(r['source_doc_id'])
            try: d['raw']=json.loads(d.pop('raw_json'))
            except Exception: pass
            docs.append(d)
            if r['status'] not in ('success','duplicate','restricted'):
                pending.append(d)
            case_key='|'.join([r['case_no'] or '',r['court'] or '',r['decision_date'] or ''])
            cases.setdefault(case_key, {'case_key':case_key,'primary_case_no':r['case_no'],'primary_court':r['court'],'decision_date':r['decision_date'],'document_ids':[],'topics':set()})
            cases[case_key]['document_ids'].append(r['source_doc_id']); cases[case_key]['topics'].update(d['topics'])
        atomic_write_text(idx/'documents.jsonl',''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in docs))
        outcases=[]
        for c in cases.values(): c['topics']=sorted(c['topics']); outcases.append(c)
        atomic_write_text(idx/'cases.jsonl',''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in outcases))
        atomic_write_text(run/'pending_retry.jsonl',''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in pending))
        json_dump(run/'checkpoint_latest.json', {'updated_at':now_iso(),'stats':self.state.stats(),'documents':len(docs),'cases':len(outcases)})
