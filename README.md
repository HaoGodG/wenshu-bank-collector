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

程序按日期切片本身倒序推进：**先处理更新的右半区间，再处理更早的左半区间**。叶子内部使用 `s51:desc` 裁判日期排序。

只有“单日仍然 >600”时才使用法院省份/法院层级/案件类型等 facet 继续细分。

## 日期协议

真实 HAR 与失败请求诊断已经确认：

- 网页直接发送用户选择的日期范围，例如 `2026-03-16 TO 2026-04-01`；
- 后端把有效日期条件表示为 `s31 GREATER ...` + `s31 LESS ...`；
- 同一查询结果中可以出现结束日当天文书，因此日期切片原样发送，不再 +/-1。

```text
2026-03-17 ~ 2026-03-31
        ↓
cprq=2026-03-17 TO 2026-03-31
```

### 已定位的日期丢失差异

失败诊断日志显示，程序原先的页面流程是：

```text
打开 ?pageId=...
→ 页面自动 queryDoc(queryCondition=[])
→ 程序再 direct queryDoc(s17+cprq)
→ HTTP 200，但后端只保留 s17，cprq 被静默丢弃
```

而成功手工 HAR 是：

```text
打开 ?pageId=...&cprqStart=...&cprqEnd=...&s17=银行
→ 页面 onload 从 URL 恢复条件
→ addParams(...)
→ loadData()
→ 第一笔 queryDoc 就带 s17+cprq
→ 后端返回 s31 日期条件
```

官网页面源码也明确按这个顺序初始化检索状态。

新的手工排序 HAR 已把“`s51` 本身导致日期丢失”排除：同一日期条件下，`s50:desc`、`s51:desc`、`s51:asc` 都返回相同 `resultCount=458`，并且后端始终保留两个 `s31`。其中 `s51:desc` 第一页均为 `2026-04-01`，`s51:asc` 第一页均为 `2026-03-16`，排序行为本身正常。

新的“从登录到排序并切到每页 15 条”HAR 已经进一步确认：日期是在 `pageSize=15` 出现的第一笔请求中立即丢失，且当时排序仍是 `s50:desc`。与上一笔成功请求逐字段比较，除每次都会变化的 `ciphertext` 外，唯一业务参数差异就是新增 `pageSize=15`。因此当前站点下，`cprq + pageSize=15` 是已复现的日期丢失触发组合，和 `s51` 无关。当前实现固定日期页大小为 `5`，并在浏览器层阻止日期请求使用其他页大小。

任何路径都必须经过 `queryParams.queryItemList` 校验，只有后端真实返回对应 `s31` 才继续采集。

### 日期问题诊断

```bash
python main.py probe-date --start-date 2026-03-17 --end-date 2026-03-31
```

不下载文书。真实请求和解密后的关键响应继续追加到：

```text
data/03_采集运行记录/query_debug.jsonl
```

日志带 `run_id` 与时间戳，不记录 Cookie；`ciphertext` 只记录长度和 SHA-256。

## 本地数据作为续跑与去重依据

默认：

```yaml
resume_from_local_latest: false
skip_completed_slices: true
```

当前仍暂时保持 `resume_from_local_latest: false`。原因不是 `s51` 不可用，而是先让新的“官网原生 loadData + s51 + pageSize=5”链路完成真实 probe 验证，再恢复按本地最大裁判日期裁剪远端范围。

当前续跑策略分两层：

- **切片级 checkpoint**：状态为 `completed` 且结束日期早于今天的历史切片，下一次运行直接整体跳过，不再重复翻页；
- **文书级去重**：对于上次未完成的切片，仍从第一页重新查询，但已成功的相同 `docId` 会在下载前直接跳过；不同 `docId` 再用高置信元数据去重。

今天的切片和未完成切片不会因为 checkpoint 被跳过，因此仍以完整性优先。

## 登录

登录态仍优先复用 `.browser-profile/`。只有登录态失效时才进入统一账号登录流程。

根据真实 HAR，文书网登录页会在 iframe 中打开 `account.court.gov.cn` 的 OAuth 登录页。程序现在会：

1. 打开文书网官方登录页；
2. 等待带 `back_url` 的统一账号 OAuth iframe；
3. 自动填写账号和密码并点击“登录”；
4. 官网自行完成密码加密并发起验证码；
5. 用户只需在浏览器中手工完成官方验证码；
6. OAuth 回跳文书网后，程序自动检测登录态并继续，不再需要回控制台按 Enter。

程序不会直接重放 `/api/login`，也不会绕过验证码。

推荐使用本地覆盖配置。先复制：

```bash
cp config.local.example.yaml config.local.yaml
```

然后只在本机的 `config.local.yaml` 填一次：

```yaml
browser:
  login_username: "你的账号或手机号"
  login_password: "你的密码"
```

`config.local.yaml` 已加入 `.gitignore`，不会提交到 Git。程序启动时会自动把它覆盖到公共的 `config.yaml` 上。

登录凭据读取顺序为：

1. `config.local.yaml` 中的 `login_username/login_password`；
2. 环境变量 `WENSHU_USERNAME/WENSHU_PASSWORD`；
3. 前两者都没有时才进入交互输入。

这样配置一次后，登录态失效时程序会自动填写账号密码并点击登录，正常情况下只需要手工完成官方验证码。

## 使用

先验证主体条件：

```bash
python main.py probe
```

只验证一个日期切片（不下载文书）：

```bash
python main.py probe-date --start-date 2026-03-17 --end-date 2026-03-31
```

例如 `--start-date 2026-03-17 --end-date 2026-03-31` 会原样验证 `cprq=2026-03-17 TO 2026-03-31`，不会再扩张一天。

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

本地 `documents` 表仍是去重权威来源。下载前按两层判断：

1. **docId 精确去重**：相同 `source_doc_id` 已成功/重复/受限时直接跳过；
2. **高置信元数据去重**：不同 docId 但“案号 + 法院 + 裁判日期 + 规范化完整标题”完全一致，并且本地已有成功/重复记录时，直接标记为重复，不再调用下载接口。

只有上述两层都未命中才执行 `isDown` 和 Word 下载。下载后的 SHA-256 去重仍保留，
用于兜底识别“不同 docId、元数据又不完全一致但文件实际相同”的情况。

`slices` 只作为查询切片审计与诊断记录。

## 从旧版本续跑

如果需要沿用旧版本已经下载的数据，请把旧项目的：

```text
data/
```

复制到 v0.4.0 项目根目录。程序会继续使用原 `collector.sqlite3` 中的 `docId/status` 去重，并读取其中成功下载记录的最大裁判日期作为回溯上界。

`.browser-profile/` 可按需复制以复用登录态；不复制也可以重新正常登录。
