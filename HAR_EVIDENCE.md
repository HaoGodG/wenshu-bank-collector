# bank-core-date.har 真实请求证据（v0.4.0 分支修复）

本文件记录本次附件 `bank-core-date(1).har` 中可以直接确认的请求行为，以及采集器据此做出的修正。

## 1. 银行当事人

真实请求中，银行主体条件同时出现在：

```text
s17=银行
queryCondition=[{"key":"s17","value":"银行"}, ...]
```

采集器继续保留顶层 `s17` 作为兼容字段，同时以 `queryCondition` 作为实际检索条件集合。

## 2. 日期条件以 queryCondition.cprq 为准

HAR 中初始页面 URL 带有：

```text
cprqStart=2026-09-08
cprqEnd=2026-09-15
s17=银行
```

第一次日期检索时，请求为：

```text
cprqStart=2026-09-08
cprqEnd=2026-09-15
queryCondition=[
  {"key":"cprq","value":"2026-09-08 TO 2026-09-15"},
  {"key":"s17","value":"银行"}
]
```

随后网页清除日期条件时，顶层 `cprqStart/cprqEnd` 仍然保持
`2026-09-08/2026-09-15`，但 `queryCondition` 已只剩：

```text
[{"key":"s17","value":"银行"}]
```

之后再次设置新的日期范围时，HAR 出现：

```text
cprqStart=2026-09-08
cprqEnd=2026-09-15
queryCondition=[
  {"key":"s17","value":"银行"},
  {"key":"cprq","value":"2026-08-04 TO 2026-09-30"}
]
```

以及：

```text
cprqStart=2026-09-08
cprqEnd=2026-09-15
queryCondition=[
  {"key":"s17","value":"银行"},
  {"key":"cprq","value":"2026-03-30 TO 2026-09-30"}
]
```

这说明顶层 `cprqStart/cprqEnd` 会残留旧页面 URL 中的值，不能作为当前日期过滤条件的权威来源；实际变化的日期过滤条件是 `queryCondition` 中的 `cprq`。

## 3. 顶层旧参数的来源

同一份 HAR 中加载的站点 `website.js` 显示，`$.WebSite.getData` 默认：

```javascript
readUrlParam: true
```

并在发送请求前执行：

```javascript
postData = $.WebSite.getParameter(postData)
```

而 `getParameter` 会先读取 `location.search`，再把调用方显式参数合并进去。

因此，当页面 URL 中仍有旧的 `cprqStart/cprqEnd` 时，即使当前检索只在
`queryCondition` 中更新 `cprq`，这些旧日期仍会被自动带到 POST 顶层。

## 4. 本分支修复

本分支不再把 `cprq` 主动复制为顶层 `cprqStart/cprqEnd`，并在程序化调用
`$.WebSite.getData` 时设置：

```javascript
readUrlParam: false
```

同时显式补充当前页面的 `pageId`，继续保留顶层 `s17=银行` 兼容字段。

修复后的请求职责为：

```text
pageId=<当前页面>
s17=银行
queryCondition=[..., {"key":"cprq","value":"A TO B"}]
```

日期条件只由当前切片生成的 `queryCondition.cprq` 控制，不再受浏览器地址栏中旧日期参数污染。

## 5. 现有日期边界转换

v0.4.0 现有逻辑仍把逻辑闭区间 `[start,end]` 转换为：

```text
(start - 1 day) TO (end + 1 day)
```

并由运行时的后端 `queryParams.queryItemList` 校验日期条件是否按预期生效。
本次附件直接证明的是“活动日期条件应以 `queryCondition.cprq` 为准”和“顶层日期参数可能陈旧”；
本次修复不额外改变既有的日期边界语义。
