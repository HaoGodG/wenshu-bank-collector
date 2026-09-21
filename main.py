from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

import yaml

from collector.models import Condition, QuerySeed
from collector.state import StateDB
from collector.site_client import WenshuBrowser, FatalAccessRestriction
from collector.runner import CollectorRunner
from collector.exporter import Exporter
from collector.planner import with_date_condition

ROOT = Path(__file__).resolve().parent
VERSION = '0.4.0'


def load_config(path: Path):
    cfg = yaml.safe_load(path.read_text(encoding='utf-8'))
    out = (cfg.get('storage') or {}).get('output_root') or ''
    output = Path(out).expanduser() if out else ROOT / 'data'
    output.mkdir(parents=True, exist_ok=True)
    return cfg, output


def query_debug_path(output: Path) -> Path:
    return output / '03_采集运行记录' / 'query_debug.jsonl'


def make_browser(cfg, output):
    return WenshuBrowser(cfg['browser'], ROOT, query_debug_path(output))


def seeds_from(cfg):
    out = []
    for q in cfg['collection'].get('query_seeds', []):
        if q.get('enabled', True) is False:
            continue
        out.append(QuerySeed(q['name'], list(q.get('topics', [])), [
            Condition(str(x['key']), str(x['value'])) for x in q.get('conditions', [])
        ]))
    return out


async def collect(cfg, output):
    state = StateDB(output / '03_采集运行记录' / 'collector.sqlite3')
    browser = make_browser(cfg, output)
    try:
        await browser.start()
        await browser.ensure_login()
        seeds = seeds_from(cfg)
        limit = min(int(cfg['collection'].get('site_visible_limit', 600)), 600)
        print(f"登录态已确认。开始采集：{len(seeds)} 组主体检索条件；单一查询窗口最多 {limit} 条，超限按年份定位后递归日期二分；单日仍超限再 facet 拆分。")
        runner = CollectorRunner(cfg['collection'], output, state, browser)
        for i, seed in enumerate(seeds, 1):
            print(f"\n##### 主体检索 {i}/{len(seeds)} #####")
            await runner.run_seed(seed)
        Exporter(output, state).export_all()
        print("\n采集计划执行完成。", state.stats())
    except FatalAccessRestriction as e:
        print(f"\n站点明确访问限制：{e}\n已保留 checkpoint，不进行规避。", file=sys.stderr)
    finally:
        Exporter(output, state).export_all()
        state.close()
        await browser.stop()


async def probe_date(cfg, output, start_text: str, end_text: str):
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    if start > end:
        raise ValueError('start-date 不能晚于 end-date')

    browser = make_browser(cfg, output)
    state = StateDB(output / '03_采集运行记录' / 'collector.sqlite3')
    try:
        await browser.start()
        await browser.ensure_login()
        seeds = seeds_from(cfg)
        if not seeds:
            print('没有配置 query_seeds')
            return
        seed = seeds[0]
        probe_collection_cfg = dict(cfg['collection'])
        probe_collection_cfg['query_condition_verify_retries'] = 1
        runner = CollectorRunner(probe_collection_cfg, output, state, browser)
        conditions = with_date_condition(seed.conditions, start, end)
        wire = next((c.value for c in conditions if c.key == 'cprq'), '')
        print(f'日期切片验证: logical={start} ~ {end}')
        print(f'wire cprq={wire}')
        print(f'排序: {runner.sort}')
        print('请求方式: 新日期切片先按官网 URL 初始化，再严格使用网页 loadData 原生排序/分页链路。')
        print('网络诊断日志:', query_debug_path(output))
        print('诊断 run_id:', browser.debug_run_id)
        print('probe-date 不下载文书；当前请求形态按手工成功排序 HAR：s51 + pageSize=5 + 官网 loadData。')

        data = await runner.query_checked(conditions, 1)
        qp = (data or {}).get('queryParams') or {}
        qr = (data or {}).get('queryResult') or {}
        rows = qr.get('resultList') or []
        print('后端 queryItemList:', qp.get('queryItemList'))
        print('resultCount:', qr.get('resultCount'))
        print('第一页前5条裁判日期:', [x.get('31') for x in rows[:5]])
        print('PROBE-DATE PASS：日期条件已被后端实际应用。')
    finally:
        state.close()
        await browser.stop()


async def doctor(cfg, output):
    browser = make_browser(cfg, output)
    try:
        await browser.start()
        info = await browser.ensure_login()
        print("网站脚本与登录态正常。", bool(info))
    finally:
        await browser.stop()


async def probe(cfg, output):
    browser = make_browser(cfg, output)
    state = StateDB(output / '03_采集运行记录' / 'collector.sqlite3')
    try:
        await browser.start()
        await browser.ensure_login()
        seeds = seeds_from(cfg)
        if not seeds:
            print('没有配置 query_seeds')
            return
        seed = seeds[0]
        runner = CollectorRunner(cfg['collection'], output, state, browser)
        print(f"验证种子: {seed.name}")
        print('请求条件:', runner.cond_dicts(seed.conditions))
        print('请求方式: 同时发送顶层条件参数 + queryCondition（与浏览器真实 HAR 一致）')
        data = await runner.query_checked(seed.conditions, 1)
        runner.verify_seed_conditions(seed.name, seed.conditions, data)
        qp = (data or {}).get('queryParams') or {}
        qr = (data or {}).get('queryResult') or {}
        rows = qr.get('resultList') or []
        print('后端 queryItemList:', qp.get('queryItemList'))
        print('resultCount:', qr.get('resultCount'))
        print('第一页前5条裁判日期:', [x.get('31') for x in rows[:5]])
        print('第一页前5条当事人字段:', [x.get('17') or x.get('53') for x in rows[:5]])
        years = await runner._years(seed.conditions)
        print('裁判年份 facet（前10个）:', years[:10])
        print('PROBE PASS：当事人检索条件已被后端接受；本命令未下载任何文书。')
    finally:
        state.close()
        await browser.stop()


def status(output):
    state = StateDB(output / '03_采集运行记录' / 'collector.sqlite3')
    try:
        print(state.stats())
        print('本地成功下载记录最大裁判日期:', state.latest_success_decision_date())
        Exporter(output, state).export_all()
    finally:
        state.close()


def show_seeds(cfg):
    seeds = seeds_from(cfg)
    print(f'共 {len(seeds)} 组启用检索条件：')
    for i, s in enumerate(seeds, 1):
        cond = ' AND '.join(f'{c.key}={c.value}' for c in s.conditions)
        print(f'{i:03d}. {s.name} | {cond} | {"/".join(s.topics)}')


def main():
    ap = argparse.ArgumentParser(description='中国裁判文书网银行当事人全量采集器')
    ap.add_argument('command', nargs='?', default='collect', choices=['collect', 'doctor', 'probe', 'probe-date', 'status', 'export', 'seeds'])
    ap.add_argument('--config', default='config.yaml')
    ap.add_argument('--start-date')
    ap.add_argument('--end-date')
    args = ap.parse_args()
    cfg, output = load_config(ROOT / args.config)
    print(f'wenshu-bank-collector v{VERSION}')
    if args.command == 'collect':
        asyncio.run(collect(cfg, output))
    elif args.command == 'doctor':
        asyncio.run(doctor(cfg, output))
    elif args.command == 'probe':
        asyncio.run(probe(cfg, output))
    elif args.command == 'probe-date':
        if not args.start_date or not args.end_date:
            ap.error('probe-date 需要 --start-date 和 --end-date')
        asyncio.run(probe_date(cfg, output, args.start_date, args.end_date))
    elif args.command == 'seeds':
        show_seeds(cfg)
    else:
        status(output)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已停止。已下载文件与 SQLite 切片状态均已保留；下次运行会跳过已完成的历史切片并继续。')
