# 裁判文书网日期 HAR 证据（v0.4.0 分支）

本文件记录两份真实浏览器 HAR 对日期检索链路的结论：

- `bank-core-date(1).har`
- `bank-core-date-0316-0401.har`

## 1. 2026-03-16 TO 2026-04-01 本身是有效条件

手工在网页选择 `2026-03-16 ~ 2026-04-01` 后，真实 queryDoc 请求包含：

```text
cprqStart=2026-03-16
cprqEnd=2026-04-01
s17=银行
queryCondition=[
  {"key":"cprq","value":"2026-03-16 TO 2026-04-01"},
  {"key":"s17","value":"银行"}
]
```

解密后的后端响应明确包含：

```text
s31 GREATER 2026-03-16
s31 LESS    2026-04-01
s17 EQUAL   银行某
resultCount=458
```

因此程序日志中同一个 `cprq=2026-03-16 TO 2026-04-01` 被后端完全丢弃，
不是因为该日期字符串非法。

## 2. 程序报错发生在后端条件解析阶段

失败日志中的后端 `queryItemList` 只有：

```text
s17 EQUAL 银行某
```

而没有任何 `s31`。这意味着 queryDoc 本身返回了成功响应，但日期条件没有进入
后端实际查询条件。运行时 verifier 正确阻止了这种响应继续被当成目标切片处理。

## 3. queryCondition.cprq 是活动日期条件，但浏览器请求仍带顶层日期

旧 HAR 证明，页面内重新选择日期后，URL 中的顶层
`cprqStart/cprqEnd` 可能仍保留旧值，而 `queryCondition.cprq` 已变化；
后端最终按新的 `queryCondition.cprq` 解析日期。

因此：

- `queryCondition.cprq` 是活动条件；
- 顶层 `cprqStart/cprqEnd` 不是权威来源；
- 但真实浏览器 queryDoc 请求始终会携带顶层日期字段。

分支实现采用折中方式：`readUrlParam=false`，不继承地址栏旧值；
同时从当前 `queryCondition.cprq` 显式生成当前的
`cprqStart/cprqEnd`，最大程度贴近浏览器请求形态。

## 4. 日期范围不应再做 +/-1 天扩张

新 HAR 查询 `2026-03-16 TO 2026-04-01` 的结果列表中直接出现
裁判日期 `2026-04-01` 的文书。

因此不能把后端返回的操作名 `LESS` 简单解释为严格数学意义上的 `<`。
网页本身就是把用户选择的起止日期原样发送。

分支现在将逻辑切片：

```text
2026-03-17 ~ 2026-03-31
```

直接发送为：

```text
cprq=2026-03-17 TO 2026-03-31
```

不再扩张成 `2026-03-16 TO 2026-04-01`。这样也避免相邻日期切片人为重叠。

## 5. /api/fp/cprq

网页源码在高级检索日期提交时会 POST `/api/fp/cprq`，源码注释将其描述为
“记录日志”。分支继续模拟这一步以贴近真实网页链路，但不把它当作日期条件语义的唯一依据。

当 queryDoc 连续两次成功返回但丢失日期条件时，分支会：

1. 重新执行日期预提交；
2. 刷新检索页；
3. 获取新的 `pageId`；
4. 再发送当前切片。

避免在同一个异常页面上下文中机械重发三次完全相同的请求。


## 6. 失败程序请求与成功浏览器请求的 Referer 上下文差异

新一轮 `probe-date` 已验证：即使重新执行 `/api/fp/cprq`，并在连续失败后刷新检索页换新的
`pageId`，后端仍稳定只接受 `s17`，完全丢弃 `cprq`。因此“坏 pageId/页面状态”不足以解释问题。

两份成功 HAR 中，queryDoc 的请求 Referer 都来自带日期参数的检索页 URL，例如：

```text
.../181217BMTKHNT2W0/index.html
?pageId=9a112334...
&cprqStart=2026-03-16
&cprqEnd=2026-04-01
&s17=银行
```

而采集器此前通过 `open_search_page()` 打开的页面只有：

```text
.../181217BMTKHNT2W0/index.html?pageId=<随机值>
```

随后虽然 POST body 中已经补齐当前 `cprqStart/cprqEnd`、`s17` 和
`queryCondition.cprq`，XHR 的页面 Referer 仍缺少日期上下文。

另外，旧 HAR 中用户在页面内把日期从 `2026-09-08~2026-09-15` 改成其他范围时，
Referer 里的日期值可以继续是旧值，但 `queryCondition.cprq` 仍能生效。这说明服务端不一定要求
Referer 日期值与当前条件完全相等，但“日期上下文存在于检索页 URL”是所有已观测成功日期请求的共同特征。

因此本分支在每次 query/facet 前使用 `history.replaceState` 将**当前**
`cprqStart/cprqEnd` 与 `s17` 写入当前文档 URL。该操作不会导航，不会新建会话，
但会使随后 XHR 的 Referer 与成功浏览器请求保持同一结构。

这一步是基于目前两份 HAR 与失败 probe 的最强剩余差异做出的修复；是否就是最终根因，
仍需用同一 `probe-date` 在真实登录态下验证。
