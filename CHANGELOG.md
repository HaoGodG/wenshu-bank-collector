# Changelog

## v0.4.0 branch fix — date slicing / diagnostics

- 新 HAR 证明 `2026-03-16 TO 2026-04-01` 可被后端正常解析并返回 458 条，日期值本身不是失败原因。
- 日期切片不再做 +/-1 天扩张，逻辑区间按网页原样发送，避免相邻切片重叠。
- 恢复并保留历史实际采集已经成功过的 direct `queryDoc` 路径；不再把“原生模块链必须替代 direct queryDoc”作为结论。
- 保留顶层 `s17/cprqStart/cprqEnd` + `queryCondition` 的请求形态，并继续对后端 `queryItemList` 做 fail-closed 校验。
- 移除未被证据支持的日期预提交、Referer/pageId 恢复等假设性修复逻辑。
- 新增 `probe-date --start-date ... --end-date ...`，单独验证指定日期切片且不下载文书。
- 新增自动 `queryDoc` 诊断日志 `data/03_采集运行记录/query_debug.jsonl`；请求发出时即落盘，Chrome 后续崩溃也能保留证据。
- 诊断日志带 `run_id` 和时间戳；不记录 Cookie，ciphertext 只记录长度与 SHA-256。
- 新增下载前高置信元数据去重：案号 + 法院 + 裁判日期 + 规范化完整标题全部一致时，跨 docId 直接跳过下载。
- 保留下载后 SHA-256 去重作为最终兜底。

## v0.4.0

- 根据 `bank-core-date.har` 确认真实日期协议：`cprq=A TO B` -> 后端 `s31 GREATER A` + `s31 LESS B`。
- 顶层日期参数按网页请求使用 `cprqStart/cprqEnd`，`queryCondition` 保留 `cprq`。
- 删除逐日扫描主策略；年份超过 600 后改为递归日期二分。
- 日期二分按时间倒序先处理更新的右半段。
- 仅当单日仍超过 600 时才进入 facet 细分。
- 去重以本地 `documents.source_doc_id/status` 为准，不再默认依赖 completed slice 跳过。
- 新增 `resume_from_local_latest`：本地成功下载记录最大裁判日期作为回溯上界。
- `status` 增加本地最大裁判日期显示。

> 注：v0.4.0 最初曾按 `GREATER/LESS` 标签做 +/-1 天扩张；后续真实 HAR 证明结束日当天仍可返回，因此本分支已取消该扩张。

## v0.3.3

- 银行主体检索使用 `s17=银行`。
- 兼容后端将 `银行` 规范化为 `银行某`。
