# Changelog

## v0.4.0 branch fix — date slicing / diagnostics

- 登录凭据支持本地 `config.local.yaml` 覆盖：优先读取 `login_username/login_password`，其次环境变量；`config.local.yaml` 已加入 `.gitignore`，避免把明文账号密码提交到仓库。\n- 开启历史完成切片 checkpoint：仅当切片已 `completed` 且结束日期早于今天时直接跳过，减少重启后的重复翻页；未完成/当天切片仍重新查询并由 docId 去重兜底。\n- 根据完整登录 HAR 增加统一账号自动填充：优先复用 browser profile；失效时等待 `account.court.gov.cn` OAuth iframe，自动填写账号密码并点击登录，官方验证码仍由用户手工完成，OAuth 回跳后程序自动继续。账号密码只从环境变量或控制台读取，不写入仓库，也不直接重放 `/api/login`。\n- 正式采集发现长时间运行后页面列表模块可能暂时缺失 `loadData1545184311000`；现在只重建当前日期切片上下文并重试当前页，不再让整个采集流程退出。
- 移除对 Playwright `response.finished()` 的显式等待，避免页面切换/关闭时产生大量 `Target closed` 未回收异步任务告警。
- 新 HAR 证明 `2026-03-16 TO 2026-04-01` 可被后端正常解析并返回 458 条，日期值本身不是失败原因。
- 日期切片不再做 +/-1 天扩张，逻辑区间按网页原样发送，避免相邻切片重叠。
- 保留历史实际采集已经成功过的 direct `queryDoc` 路径，但新日期切片先按官网带条件 URL 完成页面 onload 初始化。
- 自动诊断确认失败请求已在线路上发送完整 cprq，但页面此前先以空条件初始化，后端随后静默丢弃 cprq。
- 新的手工排序 HAR 证明 `s51:desc` 与 `s51:asc` 都能正常保留日期条件，撤销“s51 是根因”的错误判断。
- 日期查询改为严格走官网 `loadData1545184311000 -> refreshModule` 原生排序/分页链路，不再对日期使用 synthetic direct queryDoc。
- 新的完整登录→检索→改每页15条 HAR 证明：`s50:desc` 下仅新增 `pageSize=15` 后，后端即从 458 条日期结果退化为 24277 条全量银行结果并丢失全部 `s31`；日期请求因此强制 `pageSize=5`。
- `resume_from_local_latest` 暂时保持关闭，待新的原生 `s51 + pageSize=5` 链路通过真实 probe 后再恢复。
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
