# 裁判文书网日期 HAR 与运行证据（v0.4.0 分支）

本文件只记录已经被真实 HAR、运行日志或本地 SQLite 支持的结论，不把未验证假设写成根因。

已分析材料：

- `bank-core-date(1).har`
- `bank-core-date-0316-0401.har`
- 真实 `probe-date` 失败日志
- 用户现有采集 SQLite

## 1. 日期条件本身有效

用户在网页高级搜索中直接同时填写“当事人=银行”和日期
`2026-03-16 ~ 2026-04-01`，成功请求包含：

```text
cprqStart=2026-03-16
cprqEnd=2026-04-01
s17=银行
queryCondition=[
  {"key":"cprq","value":"2026-03-16 TO 2026-04-01"},
  {"key":"s17","value":"银行"}
]
```

后端解密结果包含：

```text
s31 GREATER 2026-03-16
s31 LESS    2026-04-01
s17 EQUAL   银行某
resultCount=458
```

因此 `2026-03-16 TO 2026-04-01` 不是非法日期表达式。

## 2. 不再根据 GREATER / LESS 做 +/-1 天扩张

上述同一次查询的结果列表中存在裁判日期为 `2026-04-01` 的文书。

因此不能把后端返回的 `GREATER/LESS` 标签直接解释成严格数学开区间。
网页把用户选择的起止日期原样发送，本分支也保持同样语义：

```text
逻辑切片: 2026-03-17 ~ 2026-03-31
wire cprq: 2026-03-17 TO 2026-03-31
```

这样也避免相邻二分切片因 +/-1 扩张产生重叠和重复下载。

## 3. direct queryDoc 路径不能被判定为根因

历史实际采集数据已经证明，程序原有 direct `queryDoc` 路径曾成功执行大量日期切片并下载文书。
因此不能得出“direct queryDoc 天生无法处理日期”的结论。

本分支恢复并保留这条历史已成功路径：

```text
cipher()
  -> $.WebSite.getData(queryDoc)
  -> queryParams/queryResult
```

请求继续同时发送：

```text
s17=银行
cprqStart=<当前开始日期>
cprqEnd=<当前结束日期>
queryCondition=[s17 + cprq]
sortFields=s51:desc
pageNum/pageSize
ciphertext
```

日期条件仍由 `queryParams.queryItemList` fail-closed 校验：
只有后端真实返回对应 `s31` 条件才继续采集。

## 4. 已失败的 probe 只能证明“该请求实例中 cprq 被后端丢弃”

真实失败日志中，后端只返回：

```text
s17 EQUAL 银行某
```

而没有任何 `s31`。

已经尝试过但没有解决该失败的实验包括：

- 重复日期预提交；
- 换新的 `pageId`；
- 把日期同步进页面 URL / Referer 形态。

因此这些因素不能被写成已经确认的根因。

同样，也没有证据支持“必须先做一次基础银行查询”这一前置步骤：
用户手工高级搜索是直接同时填写银行和日期后成功。

截至目前，真正根因仍需要比较“程序失败的真实 queryDoc 请求”和“成功 HAR 请求”才能确认。

## 5. 自动 query 诊断

因为失败时 Chrome 可能直接崩溃，不能依赖手工 DevTools 导出 HAR。
本分支在 Playwright 请求发出的瞬间自动追加：

```text
data/03_采集运行记录/query_debug.jsonl
```

每条记录带：

- `recorded_at`
- `run_id`
- `event_type`

对 `queryDoc` 请求记录：

- URL / method
- 当前页面 URL
- Content-Type / Origin / Referer / User-Agent
- 实际 POST form
- `queryCondition`
- `cprqStart/cprqEnd`
- `s17`
- `sortFields`
- `pageNum/pageSize`
- `ciphertext` 长度与 SHA-256（不记录原文）

对已返回并解密的结果记录：

- 请求参数
- 后端 `queryItemList`
- `resultCount`
- 响应结构字段

不记录 Cookie 内容，也不记录 ciphertext 原文。

这样即使 Chrome 在请求之后崩溃，已写入磁盘的请求证据仍然保留，可直接与成功 HAR 做字段级 diff。

## 6. 当前分支保留的功能性改动

当前只保留有明确证据或明确需求支持的改动：

1. 日期二分区间原样发送，不再 +/-1；
2. 后端条件 fail-closed 校验；
3. `probe-date` 单独验证指定日期，不下载文书；
4. 下载前先按 docId 去重；
5. 不同 docId 再按“案号 + 法院 + 裁判日期 + 规范化完整标题”做高置信去重；
6. 下载后 SHA-256 去重继续兜底；
7. queryDoc 自动诊断日志。

未验证的“原生模块链必须替代 direct queryDoc”“Referer 是根因”“需要前置搜索”等假设均不再作为正式实现依据。


## 7. 2026-09-21 失败请求自动诊断：已定位到页面初始化路径差异

`run_id=95e43fc223f14d52b9c084c41fdc6469` 的自动诊断记录显示：

1. 页面打开 `?pageId=93ff...` 后，先由官网脚本自动发送：
   `queryCondition=[]`、`sortFields=s50:desc`；
2. 随后程序 direct queryDoc 明确发送了：
   - `s17=银行`
   - `cprqStart=2026-03-17`
   - `cprqEnd=2026-03-31`
   - `queryCondition=[s17 + cprq]`
   - `sortFields=s51:desc`
3. 该请求 HTTP 200，但解密结果为：
   - `resultCount=24279`
   - `queryItemList` 只有 `s17 EQUAL 银行某`
   - 没有任何 `s31`

所以失败不是“Python 没有把日期字段发出去”，而是“日期字段已经在线路上，后端仍没有把它纳入实际查询条件”。

成功的 `03-16~04-01` 手工 HAR 则不同：

1. 浏览器先 GET：
   `?pageId=...&cprqStart=2026-03-16&cprqEnd=2026-04-01&s17=银行`
2. 页面初始化时第一笔 queryDoc 就带日期与银行条件；
3. 后端返回两个 `s31` 条件和 `resultCount=458`。

官网 `index.js?v=1.6` 的 onload 代码也明确：

```text
如果当前 pageId 没有 localStorage 恢复项：
  addParams1545035259000($.WebSite.getParameter())
然后：
  loadData()
```

即日期条件会先进入页面“已选条件”模块，再由 `loadData1545184311000` 生成实际列表请求。

因此当前修复不再把日期切片当成纯无状态 XHR 参数切换。每个新日期切片会先用带条件的搜索页 URL 让官网 onload 初始化一次；之后 direct queryDoc 仍可继续用于排序/分页。若 direct queryDoc 再次静默丢日期，则同一次运行自动回退到当前页面的官网 `loadData1545184311000`，并继续 fail-closed 验证后端 `s31`。


## 8. 2026-09-21 第二次 probe：排序字段是当前可复现触发条件

`run_id=a34c4cd339c04d7a83dbdab15da982f9` 给出了同一页面内的对照：

### 官网页面初始化（成功）

```text
pageId=300e6f6a...
cprqStart=2026-03-17
cprqEnd=2026-03-31
s17=银行
sortFields=s50:desc
queryCondition=[cprq, s17]
```

后端：

```text
s31 GREATER 2026-03-17
s31 LESS    2026-03-31
s17 EQUAL   银行某
resultCount=386
```

说明页面初始化修复是有效的，日期条件已经真正进入后端。

### 随后 direct queryDoc（失败）

同一个 pageId、同一个 Referer、同一组日期与银行条件，改为：

```text
sortFields=s51:desc
pageSize=15
```

后端立即退化为：

```text
s17 EQUAL 银行某
resultCount=24279
```

日期 `s31` 消失。

### 官网 loadData fallback（同样失败）

随后调用官网自己的 `loadData1545184311000`，请求仍带正确
`cprqStart/cprqEnd/s17/queryCondition`，但只要 `sortFields=s51:desc`，
后端仍只保留 `s17`。

### 排除 pageSize 与 queryCondition 顺序

旧 HAR 还有一个真实请求：

```text
sortFields=s50:desc
pageSize=5
queryCondition=[s17, cprq]
```

该日期条件被后端正常接受。

因此 `pageSize` 和条件顺序都不能解释本次差异。当前真实环境中，可重复观察到的关键触发字段是 `sortFields=s51:desc`。

正式日期采集因此固定使用官网默认 `s50:desc`，并在浏览器层阻止带 `cprq` 的请求使用 `s51:*`。
