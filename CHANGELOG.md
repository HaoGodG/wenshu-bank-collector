# Changelog

## v0.4.0 branch fix — HAR request isolation

- 根据 `bank-core-date(1).har` 修正日期请求组装：活动日期只使用 `queryCondition.cprq`。
- 禁止 `$.WebSite.getData` 自动合并当前 URL 参数（`readUrlParam: false`），避免旧 `cprqStart/cprqEnd` 污染日期切片。
- 显式保留当前 `pageId`，并继续镜像顶层 `s17=银行`。
- 新增回归测试，确保 `cprq` 不再被复制到顶层日期参数。

## v0.4.0

- 根据 `bank-core-date.har` 重新确认真实日期协议：`cprq=A TO B` -> `s31 GREATER A` + `s31 LESS B`。
- 恢复逻辑闭区间到 wire 开区间的 +/-1 天转换，防止边界日期漏采。
- 顶层日期参数按网页请求使用 `cprqStart/cprqEnd`，`queryCondition` 保留 `cprq`。
- 删除逐日扫描主策略；年份超过 600 后改为递归日期二分。
- 日期二分按时间倒序先处理更新的右半段。
- 仅当单日仍超过 600 时才进入 facet 细分。
- 去重以本地 `documents.source_doc_id/status` 为准，不再默认依赖 completed slice 跳过。
- 新增 `resume_from_local_latest`：本地成功下载记录最大裁判日期作为回溯上界。
- `status` 增加本地最大裁判日期显示。

## v0.3.3

- 银行主体检索使用 `s17=银行`。
- 兼容后端将 `银行` 规范化为 `银行某`。
