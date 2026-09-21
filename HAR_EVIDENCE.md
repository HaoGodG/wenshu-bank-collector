# bank-core-date.har 真实请求证据（v0.4.0）

用户在网页中手工执行“当事人=银行 + 日期”后导出的 HAR 显示：

## 1. 银行当事人

请求同时包含：

```text
s17=银行
queryCondition=[{"key":"s17","value":"银行"}, ...]
```

后端成功解析后会把当事人值匿名化为：

```text
s17 EQUAL 银行某
```

## 2. 日期

真实请求示例：

```text
cprqStart=2026-09-08
cprqEnd=2026-09-15
queryCondition=[{"key":"cprq","value":"2026-09-08 TO 2026-09-15"},{"key":"s17","value":"银行"}]
```

解密后的后端 `queryItemList` 为：

```text
s31 GREATER 2026-09-08
s31 LESS    2026-09-15
s17 EQUAL   银行某
```

因此 `cprq` 在后端是开区间。v0.4.0 对逻辑闭区间 `[start,end]` 自动发送：

```text
(start - 1 day) TO (end + 1 day)
```

例如逻辑 `2026-01-01 ~ 2026-06-30` 实际 wire 条件为：

```text
2025-12-31 TO 2026-07-01
```

从而保留 1 月 1 日与 6 月 30 日两个边界日。
