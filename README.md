# wenshu-bank-collector v0.4.0

目标：下载裁判文书网中**银行作为当事人**的公开文书，并规避单查询只能访问前 600 条造成的漏数。

## 核心检索

```text
s17=银行
```

`s17` 是当事人字段。真实 HAR 中后端会把 `银行` 规范化为 `银行某`，程序已兼容这一站点行为。

## 600 条以上：日期二分，不再逐日扫

先用年份 facet 定位年份。一个年份的实际日期查询如果超过 600，递归二分：

```text
2026-01-01 ~ 2026-12-31
        ↓
2026-01-01 ~ 2026-06-30
2026-07-01 ~ 2026-12-31
```

如果某一半仍超过 600，就继续对该半区间二分，直到每个叶子查询 `<=600`。

因为正式下载按裁判日期倒序，程序总是**先处理更新的右半区间，再处理更早的左半区间**。

只有“单日仍然 >600”时才使用法院省份/法院层级/案件类型等 facet 继续细分。

## 日期协议

`bank-core-date(1).har` 中可以直接确认：网页后续修改日期时，顶层 `cprqStart/cprqEnd` 可能仍保留页面 URL 中的旧值，而当前日期条件随 `queryCondition.cprq` 变化。

因此本分支调用站点接口时关闭 URL 参数自动合并（`readUrlParam: false`），显式保留 `pageId` 和顶层 `s17=银行`，**日期切片只由 `queryCondition.cprq` 控制**。

v0.4.0 既有运行时后端校验会检查 `cprq` 是否被解析为：

```text
cprq=A TO B
```

转换为：

```text
s31 GREATER A
s31 LESS B
```

因此程序内部把逻辑闭区间自动扩展一天。例如逻辑：

```text
2026-01-01 ~ 2026-06-30
```

实际活动查询条件发送为：

```text
queryCondition=[..., {"key":"cprq","value":"2025-12-31 TO 2026-07-01"}]
```

不会再把该值复制到顶层 `cprqStart/cprqEnd`，以免与地址栏残留日期混淆。

确保首尾日期不漏。

## 本地数据作为续跑与去重依据

默认：

```yaml
resume_from_local_latest: true
skip_completed_slices: false
```

程序启动后读取本地 SQLite：

```text
data/03_采集运行记录/collector.sqlite3
```

取 `status=success` 文书中的**最大裁判日期**作为本轮回溯上界。

例如本地最新裁判日期是：

```text
2026-06-11
```

则本轮只检索：

```text
更早日期 ... ~ 2026-06-11
```

6 月 11 日当天仍会包含在检索里，已经下载过的 `docId` 会直接根据本地 `documents` 表跳过，因此不会重复下载，同时也不会因为同日还有其他文书而漏掉整天。

> 如果你希望完全从站点最新日期重新扫描，把 `resume_from_local_latest` 改成 `false` 即可。

## 使用

先验证：

```bash
python main.py probe
```

正式采集：

```bash
python main.py
```

查看本地状态和续跑上界：

```bash
python main.py status
```

## 输出

仍写入：

```text
data/
├── 00_总索引/
├── 01_案例原文/
└── 03_采集运行记录/
    └── collector.sqlite3
```

本地 `documents` 表的 `source_doc_id/status` 是去重权威来源；`slices` 只作为查询切片审计与诊断记录。

## 从旧版本续跑

如果需要沿用旧版本已经下载的数据，请把旧项目的：

```text
data/
```

复制到 v0.4.0 项目根目录。程序会继续使用原 `collector.sqlite3` 中的 `docId/status` 去重，并读取其中成功下载记录的最大裁判日期作为回溯上界。

`.browser-profile/` 可按需复制以复用登录态；不复制也可以重新正常登录。
