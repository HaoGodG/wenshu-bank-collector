from __future__ import annotations

import asyncio
import math
from datetime import date, datetime
from pathlib import Path

from .models import Condition, QuerySeed
from .parser import parse_official_doc
from .planner import add_condition, bisect_dates, with_date_condition
from .utils import safe_name, sha256_file, json_dump, now_iso
from .exporter import Exporter
from .site_client import FatalAccessRestriction

FIELD_MAP = {
    '1': 'title',
    '2': 'court',
    '7': 'case_no',
    '17': 'parties',
    '31': 'decision_date',
    '6': 'doc_type',
}


def normalize_result(item: dict) -> dict:
    meta = {name: item.get(key) for key, name in FIELD_MAP.items()}
    meta['source_doc_id'] = item.get('rowkey') or item.get('5')
    return meta


class CollectorRunner:
    """Bank-party collector using date bisection under the site's 600-row window."""

    def __init__(self, cfg, root: Path, state, browser):
        self.cfg = cfg
        self.root = root
        self.state = state
        self.browser = browser
        self.page_size = int(cfg.get('page_size', 5))
        self.site_limit = min(max(1, int(cfg.get('site_visible_limit', 600))), 600)
        self.interval = float(cfg.get('request_interval_seconds', 1.5))
        self.sort = cfg.get('sort_fields', 's51:desc')
        self.max_pages = max(1, math.ceil(self.site_limit / self.page_size))
        self.overflow_facets = list(cfg.get('overflow_facets') or ['s33', 's39', 's40', 's4', 's8', 's6'])
        # Local document rows are the dedupe authority.  Slices remain audit/checkpoint records.
        self.skip_completed_slices = bool(cfg.get('skip_completed_slices', False))
        self.resume_from_local_latest = bool(cfg.get('resume_from_local_latest', True))
        self.exporter = Exporter(root, state)
        self._verified_seed_names = set()

    async def pause(self):
        if self.interval > 0:
            await asyncio.sleep(self.interval)

    @staticmethod
    def _norm_backend_value(value):
        return str(value or '').replace('\\-', '-')

    @classmethod
    def _backend_items(cls, data):
        qp = (data or {}).get('queryParams') or {}
        items = qp.get('queryItemList')
        if not isinstance(items, list):
            return None
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get('id') or item.get('key') or '')
            value = cls._norm_backend_value(item.get('value'))
            oper = str(item.get('oper') or '').upper()
            if key:
                out.append({'id': key, 'value': value, 'oper': oper})
        return out

    @classmethod
    def _date_condition_accepted(cls, condition: Condition, accepted: list[dict]) -> bool:
        """Verify the exact HAR-proven cprq -> s31 GREATER/LESS translation."""
        raw = cls._norm_backend_value(condition.value)
        if any(x['id'] == 'cprq' and cls._norm_backend_value(x['value']) == raw for x in accepted):
            return True
        if ' TO ' not in raw:
            return False
        start, end = [x.strip() for x in raw.split(' TO ', 1)]
        gt = any(x['id'] == 's31' and x['value'] == start and x['oper'] in ('GREATER', 'GT', '>') for x in accepted)
        lt = any(x['id'] == 's31' and x['value'] == end and x['oper'] in ('LESS', 'LT', '<') for x in accepted)
        return gt and lt

    @staticmethod
    def _party_value_equivalent(requested: str, backend: str) -> bool:
        req = str(requested or '').strip()
        got = str(backend or '').strip()
        if req == got:
            return True
        # Real bank-core.har: request s17=银行; backend normalises to 银行某.
        return got.replace('某', '') == req.replace('某', '')

    @classmethod
    def check_backend_conditions(cls, requested_conditions, data):
        accepted = cls._backend_items(data)
        if accepted is None:
            return False, ['queryParams.queryItemList missing'], []
        missing = []
        for c in requested_conditions:
            key = str(c.key)
            value = str(c.value)
            if key == 'cprq':
                ok = cls._date_condition_accepted(c, accepted)
            elif key == 's17':
                ok = any(x['id'] == key and cls._party_value_equivalent(value, x['value']) for x in accepted)
            else:
                ok = any(x['id'] == key and x['value'] == value for x in accepted)
            if not ok:
                missing.append(f'{key}={value}')
        return not missing, missing, accepted

    async def _recover_browser_session(self, error, *, operation: str):
        checker = getattr(self.browser, 'is_session_lost_error', None)
        if not checker or not checker(error):
            return False
        reason = str(error)
        self.state.event('browser_session_lost', {
            'operation': operation,
            'error': reason[:1000],
        })
        print(f"  [browser-recover] {operation} -> {reason}")
        await self.browser.recover_session(reason)
        return True

    async def query_checked(self, conditions, page_num, sort_fields=None):
        attempts = max(1, int(self.cfg.get('query_condition_verify_retries', 3)))
        requested = self.cond_dicts(conditions)
        last_reason = ''
        actual_sort = sort_fields or self.sort
        raw_date = next((str(c.value) for c in conditions if c.key == 'cprq'), '')
        query_label = raw_date or '无日期条件'
        max_session_recoveries = max(1, int(self.cfg.get('browser_session_recovery_retries', 2)))
        session_recoveries = 0
        attempt = 1
        while attempt <= attempts:
            print(
                f"  [query-start] {query_label} | page={page_num} | "
                f"pageSize={self.page_size} | sort={actual_sort} | attempt={attempt}/{attempts}"
            )
            task = None
            try:
                task = asyncio.create_task(
                    self.browser.query(requested, page_num, self.page_size, actual_sort)
                )
                waited = 0
                while True:
                    done, _ = await asyncio.wait({task}, timeout=15.0)
                    if task in done:
                        data = await task
                        break
                    waited += 15
                    print(
                        f"  [query-wait] {query_label} | page={page_num} | "
                        f"已等待 {waited}s，仍在等待官网响应/页面初始化"
                    )
            except Exception as e:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                checker = getattr(self.browser, 'is_session_lost_error', None)
                if checker and checker(e):
                    if session_recoveries >= max_session_recoveries:
                        raise
                    session_recoveries += 1
                    await self._recover_browser_session(
                        e,
                        operation=f'query page={page_num} conditions={query_label}',
                    )
                    if raw_date and hasattr(self.browser, 'invalidate_date_context'):
                        self.browser.invalidate_date_context(raw_date)
                    continue
                raise

            ok, missing, accepted = self.check_backend_conditions(conditions, data)
            if ok:
                return data
            last_reason = '后端未应用: ' + ', '.join(missing)
            print(f"  [query-verify] 响应无效，重试 {attempt}/{attempts}: {last_reason}")
            print(f"  [query-verify] 后端实际 queryItemList: {accepted}")
            missing_cprq = next(
                (str(c.value) for c in conditions if c.key == 'cprq' and f'cprq={c.value}' in missing),
                None,
            )
            if missing_cprq and hasattr(self.browser, 'invalidate_date_context'):
                self.browser.invalidate_date_context(missing_cprq)
            if attempt < attempts:
                await asyncio.sleep(max(self.interval, 2.0))
            attempt += 1
        raise RuntimeError('裁判文书网连续返回未完整应用检索条件的响应，已停止当前检索以避免误采。 ' + last_reason)

    def verify_seed_conditions(self, seed_name, requested_conditions, data):
        if seed_name in self._verified_seed_names:
            return
        ok, missing, accepted = self.check_backend_conditions(requested_conditions, data)
        print(f"  后端已接受条件: {[(x['id'], x['value'], x['oper']) for x in accepted] if accepted else accepted}")
        if not ok:
            raise RuntimeError(f"后端未接受全部检索条件，已停止以避免误采。 missing={missing}, accepted={accepted}")
        self._verified_seed_names.add(seed_name)

    @staticmethod
    def cond_dicts(conditions):
        return [{'key': c.key, 'value': c.value} for c in conditions]

    @staticmethod
    def _result_count(data) -> int:
        qr = (data or {}).get('queryResult') or {}
        try:
            return max(0, int(qr.get('resultCount') or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _rows(data) -> list[dict]:
        return ((data or {}).get('queryResult') or {}).get('resultList') or []

    @staticmethod
    def _parse_date(value) -> date | None:
        text = str(value or '').strip()[:10]
        try:
            return datetime.strptime(text, '%Y-%m-%d').date()
        except ValueError:
            return None

    @classmethod
    def facet_items(cls, raw) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        if isinstance(raw, dict):
            for key, count in raw.items():
                try:
                    n = int(count or 0)
                except (TypeError, ValueError):
                    continue
                if str(key).strip() and n > 0:
                    out.append((str(key), n))
            return out
        if not isinstance(raw, list):
            return out
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                value, count = item[0], item[1]
            elif isinstance(item, dict):
                value = item.get('value') or item.get('name') or item.get('code') or item.get('id') or item.get('key')
                count = item.get('count') or item.get('num') or item.get('valueCount') or item.get('docCount')
            else:
                continue
            try:
                n = int(count or 0)
            except (TypeError, ValueError):
                continue
            if str(value or '').strip() and n > 0:
                out.append((str(value), n))
        return out

    async def _facet_items(self, conditions, field: str) -> list[tuple[str, int]]:
        raw = await self.browser.facet(self.cond_dicts(conditions), field)
        await self.pause()
        return self.facet_items(raw)

    async def _years(self, conditions) -> list[tuple[int, int]]:
        items = await self._facet_items(conditions, 's42')
        out = []
        for value, count in items:
            text = str(value).strip()
            if len(text) == 4 and text.isdigit():
                out.append((int(text), count))
        out.sort(reverse=True)
        return out

    def _slice_key(self, seed: QuerySeed, conditions, start_label: str, end_label: str):
        return self.state.slice_key(seed.name, conditions, start_label, end_label)

    def _can_skip_completed(self, key: str, immutable: bool) -> bool:
        return self.skip_completed_slices and immutable and self.state.get_slice_status(key) == 'completed'

    def _local_resume_upper_bound(self) -> date | None:
        if not self.resume_from_local_latest:
            return None
        value = self.state.latest_success_decision_date()
        return self._parse_date(value)

    async def run_seed(self, seed: QuerySeed):
        conditions = seed.conditions
        print(f"\n=== {seed.name} ===")
        print(f"  条件: {self.cond_dicts(conditions)} | 排序={self.sort} | 单查询窗口={self.site_limit}")

        first = await self.query_checked(conditions, 1)
        await self.pause()
        self.verify_seed_conditions(seed.name, conditions, first)
        total = self._result_count(first)
        print(f"  总 resultCount={total}")
        if total <= 0:
            return

        local_end = self._local_resume_upper_bound()
        if local_end:
            print(f"  本地已成功下载记录的最大裁判日期={local_end.isoformat()}")
            print(f"  本轮按倒序回溯，仅检索 <= {local_end.isoformat()}；同日已下载 docId 由本地记录去重。")

        years = await self._years(conditions)
        if years:
            print("  使用裁判年份定位区间；每个年份实际按日期区间查询，超过600递归二分。")
            for year, facet_count in years:
                start = date(year, 1, 1)
                end = date(year, 12, 31)
                if local_end:
                    if start > local_end:
                        print(f"  [local-resume] 跳过 {year}：整个年份晚于本地上界 {local_end}")
                        continue
                    end = min(end, local_end)
                if start > end:
                    continue
                print(f"\n  --- {year} 年：facet={facet_count}，逻辑区间={start} ~ {end} ---")
                await self._run_date_range(seed, conditions, start, end, depth=0)
            return

        raise RuntimeError(
            '裁判年份 facet 不可用，已停止。当前站点实测 s51 裁判日期排序会使 cprq 被后端静默丢弃，'
            '因此不再使用 s51:asc/desc 推断最早/最晚日期，避免漏采。'
        )

    async def _run_date_range(self, seed: QuerySeed, base_conditions, start: date, end: date, depth: int):
        logical_label = f'{start.isoformat()}~{end.isoformat()}'
        range_conditions = with_date_condition(base_conditions, start, end)
        wire = next((c.value for c in range_conditions if c.key == 'cprq'), '')
        key = self._slice_key(seed, range_conditions, start.isoformat(), end.isoformat())
        immutable = end < date.today()

        if self._can_skip_completed(key, immutable):
            print(f"  {'  '*depth}[skip] {logical_label} 已完成")
            return

        first = await self.query_checked(range_conditions, 1)
        await self.pause()
        total = self._result_count(first)
        print(f"  {'  '*depth}[range] {logical_label} | wire={wire} | resultCount={total}")

        if total <= 0:
            self.state.save_slice(key, seed.name, range_conditions, start.isoformat(), end.isoformat(), 'completed', 0, 'empty range')
            return

        if total <= self.site_limit:
            await self._process_leaf(seed, range_conditions, start.isoformat(), end.isoformat(), first, total, immutable)
            return

        if start < end:
            left, right = bisect_dates(start, end)
            print(
                f"  {'  '*depth}超过 {self.site_limit}，日期二分："
                f"[{left[0]} ~ {left[1]}] + [{right[0]} ~ {right[1]}]；按时间倒序先处理后半段"
            )
            # Descending collection: newer half first, then older half.
            await self._run_date_range(seed, base_conditions, right[0], right[1], depth + 1)
            await self._run_date_range(seed, base_conditions, left[0], left[1], depth + 1)
            self.state.save_slice(
                key, seed.name, range_conditions, start.isoformat(), end.isoformat(), 'completed', total,
                'date range fully partitioned by recursive bisection',
            )
            return

        print(f"  {'  '*depth}单日 {start} 仍超过 {self.site_limit}，转 facet 细分")
        await self._split_overflow(seed, range_conditions, logical_label, total, immutable, 0)
        self.state.save_slice(
            key, seed.name, range_conditions, start.isoformat(), end.isoformat(), 'completed', total,
            'single-day overflow fully partitioned by facets',
        )

    async def _split_overflow(self, seed: QuerySeed, conditions, label: str, total: int, immutable: bool, facet_index: int):
        for idx in range(facet_index, len(self.overflow_facets)):
            field = self.overflow_facets[idx]
            if any(c.key == field for c in conditions):
                continue
            items = await self._facet_items(conditions, field)
            if len(items) <= 1:
                continue
            facet_sum = sum(count for _, count in items)
            if facet_sum != total:
                print(f"    facet {field} 覆盖不完整：sum={facet_sum} != total={total}，尝试下一维度")
                continue

            print(f"    使用 facet {field} 拆成 {len(items)} 个子查询")
            for value, advertised_count in items:
                child = add_condition(conditions, field, value)
                child_label = f'{label}|{field}={value}'
                key = self._slice_key(seed, child, child_label, child_label)
                first = await self.query_checked(child, 1)
                await self.pause()
                child_total = self._result_count(first)
                if child_total <= 0:
                    self.state.save_slice(key, seed.name, child, child_label, child_label, 'completed', 0, 'facet child empty')
                    continue
                print(f"      {field}={value}: facet={advertised_count}, query={child_total}")
                if child_total <= self.site_limit:
                    await self._process_leaf(seed, child, child_label, child_label, first, child_total, immutable)
                else:
                    await self._split_overflow(seed, child, child_label, child_total, immutable, idx + 1)
                    self.state.save_slice(key, seed.name, child, child_label, child_label, 'completed', child_total, 'overflow fully partitioned by deeper facets')
            return

        key = self._slice_key(seed, conditions, label, label)
        note = f'overflow={total}; no safe facet can fully partition this query below site limit={self.site_limit}'
        self.state.save_slice(key, seed.name, conditions, label, label, 'overflow', total, note)
        self.state.event('slice_overflow_unresolved', {
            'seed_name': seed.name,
            'label': label,
            'conditions': self.cond_dicts(conditions),
            'result_count': total,
            'site_limit': self.site_limit,
        })
        raise RuntimeError(f'查询切片 {label} 仍有 {total} 条，且现有 facet 无法安全继续拆分。已记录 overflow 并停止，未把前600条冒充全量。')

    async def _process_leaf(self, seed: QuerySeed, conditions, start_label: str, end_label: str, first, total: int, immutable: bool):
        if total > self.site_limit:
            raise ValueError(f'leaf total {total} exceeds site limit {self.site_limit}')
        key = self._slice_key(seed, conditions, start_label, end_label)
        if self._can_skip_completed(key, immutable):
            print(f"    [skip] {start_label} ~ {end_label} 已完成")
            return

        self.state.save_slice(key, seed.name, conditions, start_label, end_label, 'running', total, 'leaf')
        pages = math.ceil(total / self.page_size) if total else 0
        if pages > self.max_pages:
            raise RuntimeError(f'内部保护：叶子查询需要 {pages} 页，超过允许的 {self.max_pages} 页。')
        print(f"    叶子查询 {start_label} ~ {end_label}: {total} 条，共 {pages} 页")

        processed = 0
        unresolved = 0
        for page_num in range(1, pages + 1):
            page_data = first if page_num == 1 else await self.query_checked(conditions, page_num)
            if page_num > 1:
                await self.pause()
            rows = self._rows(page_data)
            remaining = total - processed
            if remaining <= 0:
                break
            rows = rows[:remaining]
            if not rows:
                self.state.save_slice(key, seed.name, conditions, start_label, end_label, 'failed', total, f'page {page_num} unexpectedly empty after processed={processed}')
                raise RuntimeError(f'叶子查询 {start_label} ~ {end_label} 的第 {page_num} 页意外为空；为避免把不完整结果标记为完成，已停止。')

            counters = {'success': 0, 'skipped': 0, 'duplicate': 0, 'restricted': 0, 'failed': 0, 'missing': 0}
            for item in rows:
                outcome = await self.handle_result(item, seed)
                counters[outcome if outcome in counters else 'failed'] += 1
            processed += len(rows)
            unresolved += counters['failed'] + counters['missing']
            print(
                f"      page {page_num}/{pages}: {len(rows)} | 累计={processed}/{total} | "
                f"下载成功={counters['success']} 本地已存在={counters['skipped']} 重复={counters['duplicate']} "
                f"受限={counters['restricted']} 失败={counters['failed']}"
            )
            self.exporter.export_all()

        if processed < total:
            self.state.save_slice(key, seed.name, conditions, start_label, end_label, 'failed', total, f'processed={processed};expected={total}')
            raise RuntimeError(f'叶子查询 {start_label} ~ {end_label} 仅处理 {processed}/{total} 条，已停止，避免漏采。')

        if unresolved:
            self.state.save_slice(
                key, seed.name, conditions, start_label, end_label, 'failed', total,
                f'processed={processed};unresolved={unresolved};site_limit={self.site_limit};sort={self.sort}',
            )
            raise RuntimeError(
                f'叶子查询 {start_label} ~ {end_label} 有 {unresolved} 条下载失败/缺少文书ID；'
                '未标记 completed，重新运行会继续重试，避免永久漏采。'
            )

        self.state.save_slice(key, seed.name, conditions, start_label, end_label, 'completed', total, f'processed={processed};site_limit={self.site_limit};sort={self.sort}')
        self.exporter.export_all()

    async def handle_result(self, item, seed: QuerySeed):
        meta = normalize_result(item)
        doc_id = meta.get('source_doc_id')
        if not doc_id:
            return 'missing'
        self.state.upsert_seen(doc_id, meta, item, seed.topics, query_source=seed.name)
        # Layer 1: exact docId dedupe happens before any remote download call.
        if not self.state.should_download(doc_id):
            return 'skipped'

        # Layer 2: different docIds can still point to the same published
        # document.  Use only high-confidence list metadata to skip before
        # isDown/download; incomplete or non-exact metadata falls through.
        duplicate = self.state.find_pre_download_duplicate(doc_id, meta)
        if duplicate:
            canonical = duplicate['canonical_doc_id']
            self.state.mark_pre_download_duplicate(
                doc_id,
                canonical,
                'same case_no + court + decision_date + normalized title',
            )
            print(f"    [pre-dedupe] {meta.get('case_no')} -> duplicate_of={canonical}")
            return 'duplicate'

        retries = max(1, int(self.cfg.get('max_download_retries', 3)))
        max_session_recoveries = max(1, int(self.cfg.get('browser_session_recovery_retries', 2)))
        session_recoveries = 0
        attempt = 1
        while attempt <= retries:
            try:
                self.state.mark_attempt(doc_id)
                chk = await self.browser.is_down(doc_id)
                await self.pause()
                code = chk.get('code')
                if code in (1, 2, 3):
                    self.state.mark_failure(doc_id, 'restricted', f"isDown code={code}: {chk.get('msg')}")
                    return 'restricted'
                if code != 0:
                    raise RuntimeError(f"isDown code={code}: {chk.get('msg')}")
                year = (meta.get('decision_date') or 'unknown')[:4]
                court = safe_name(meta.get('court') or 'unknown')
                case_no = safe_name(meta.get('case_no') or doc_id[:16])
                docdir = self.root / '01_案例原文' / year / court / case_no / safe_name(doc_id, 64)
                docdir.mkdir(parents=True, exist_ok=True)
                tmp = docdir / 'official_original.doc.part'
                await self.browser.download_one(doc_id, tmp)
                final = docdir / 'official_original.doc'
                tmp.replace(final)
                sha = sha256_file(final)
                old = self.state.find_by_sha(sha)
                if old and old['source_doc_id'] != doc_id:
                    final.unlink(missing_ok=True)
                    self.state.mark_duplicate(doc_id, sha, old['source_doc_id'])
                    return 'duplicate'
                html_path = txt_path = None
                parse_error = None
                try:
                    html, text = parse_official_doc(final)
                    html_path = docdir / 'official_fulltext.html'
                    html_path.write_text(html, encoding='utf-8')
                    txt_path = docdir / 'official_fulltext.txt'
                    txt_path.write_text(text, encoding='utf-8')
                except Exception as pe:
                    parse_error = str(pe)
                json_dump(docdir / 'document.json', {
                    **meta,
                    'source': '中国裁判文书网',
                    'source_code': 'C1',
                    'topics': seed.topics,
                    'first_query_source': seed.name,
                    'bank_party_query': any(c.key == 's17' and '银行' in c.value for c in seed.conditions),
                    'download_status': 'success',
                    'parse_error': parse_error,
                    'collected_at': now_iso(),
                })
                json_dump(docdir / 'source_provenance.json', {
                    'source_name': '中国裁判文书网',
                    'source_domain': 'wenshu.court.gov.cn',
                    'retrieval_method': 'authenticated_party_search_date_bisect_direct_download',
                    'wenshu_doc_id': doc_id,
                    'retrieved_at': now_iso(),
                    'original_sha256': sha,
                })
                json_dump(docdir / 'hashes.json', {'official_original.doc': sha})
                self.state.mark_success(
                    doc_id,
                    str(final.relative_to(self.root)),
                    str(txt_path.relative_to(self.root)) if txt_path else None,
                    str(html_path.relative_to(self.root)) if html_path else None,
                    sha,
                )
                return 'success'
            except FatalAccessRestriction:
                raise
            except Exception as e:
                checker = getattr(self.browser, 'is_session_lost_error', None)
                if checker and checker(e):
                    if session_recoveries >= max_session_recoveries:
                        raise
                    session_recoveries += 1
                    self.state.mark_failure(doc_id, 'retry', f'browser session lost: {e}')
                    print(
                        f"    download browser recover {session_recoveries}/{max_session_recoveries}: "
                        f"{meta.get('case_no')} -> {e}"
                    )
                    await self._recover_browser_session(
                        e,
                        operation=f"download {meta.get('case_no') or doc_id}",
                    )
                    continue
                self.state.mark_failure(doc_id, 'retry', str(e))
                print(f"    download retry {attempt}/{retries}: {meta.get('case_no')} -> {e}")
                if attempt < retries:
                    await asyncio.sleep(max(self.interval, 2))
                attempt += 1
        self.state.mark_failure(doc_id, 'failed', '达到最大重试次数')
        return 'failed'
