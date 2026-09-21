from __future__ import annotations
import asyncio, json, uuid
from pathlib import Path
from urllib.parse import quote, urlparse
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

class FatalAccessRestriction(RuntimeError): pass
class SiteCallTimeout(RuntimeError): pass

class WenshuBrowser:
    def __init__(self, cfg: dict, project_root: Path):
        self.cfg = cfg
        self.project_root = project_root
        self.pw = self.context = self.page = None
        self._prepared_cprq = None

    async def start(self):
        self.pw = await async_playwright().start()
        profile = Path(self.cfg['profile_dir'])
        if not profile.is_absolute():
            profile = self.project_root / profile
        profile.mkdir(parents=True, exist_ok=True)

        kwargs = dict(
            user_data_dir=str(profile),
            headless=False,
            accept_downloads=True,
            viewport={"width": 1440, "height": 950},
            locale="zh-CN",
        )

        # 优先使用用户本机安装的正式 Chrome。若不可用，再回退到 Playwright Chromium。
        channel = str(self.cfg.get('channel') or '').strip()
        if channel:
            try:
                self.context = await self.pw.chromium.launch_persistent_context(channel=channel, **kwargs)
            except PlaywrightError as e:
                print(f"[browser] 无法使用 channel={channel}，回退 Playwright Chromium：{e}")
                self.context = await self.pw.chromium.launch_persistent_context(**kwargs)
        else:
            self.context = await self.pw.chromium.launch_persistent_context(**kwargs)

        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        # 启动阶段只负责把浏览器带到检索页。不要在尚未判断登录态前
        # 强制要求 cipher() 已加载，否则站点脚本偶发延迟会让程序直接退出。
        await self.open_search_page(require_ready=False)

    async def stop(self):
        # PyCharm Stop / Ctrl+C / 用户手工关闭 Chrome 时，driver 可能已经先断开。
        # 清理阶段不应把这种正常停止升级成新的异常。
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.pw:
            try:
                await self.pw.stop()
            except Exception:
                pass

    async def _goto(self, url: str, *, tolerate_aborted: bool = False):
        try:
            return await self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except PlaywrightError as e:
            # 部分页面在加载过程中会主动触发下一次导航，Playwright 会把前一次 goto
            # 记成 ERR_ABORTED。登录流程允许人工接管，因此这里不应直接结束整个采集器。
            if tolerate_aborted and "ERR_ABORTED" in str(e):
                await asyncio.sleep(1.0)
                print(f"[browser] 页面导航被浏览器中止（ERR_ABORTED），保留当前窗口继续判断。当前页面：{self.page.url}")
                return None
            raise

    async def open_search_page(self, *, require_ready: bool = True):
        page_id = uuid.uuid4().hex
        url = f"{self.cfg['search_url']}?pageId={page_id}"
        # 检索页本身偶尔也会因页面脚本二次导航产生 ERR_ABORTED；只要最终页面仍
        # 留在 wenshu.court.gov.cn，就交给后续 ready 检测判断，不在 goto 阶段误杀。
        await self._goto(url, tolerate_aborted=True)
        if require_ready:
            await self.wait_ready()

    async def _page_diag(self) -> dict:
        """只返回非敏感页面状态，便于判断是脚本延迟、登录页还是访问限制页。"""
        url = self.page.url if self.page else ''
        try:
            title = await self.page.title()
        except Exception:
            title = ''
        try:
            state = await self.page.evaluate("""() => ({
              jq: !!window.jQuery,
              website: !!(window.jQuery && $.WebSite),
              cipher: typeof window.cipher === 'function',
              readyState: document.readyState
            })""")
        except Exception:
            state = {}
        return {"url": url, "title": title, **(state or {})}

    async def _wait_base_ready(self, timeout_seconds: int = 15) -> bool:
        """登录态检测只需要 jQuery + WebSite，不要求 queryDoc 使用的 cipher 已就绪。"""
        try:
            await self.page.wait_for_function(
                "window.jQuery && $.WebSite",
                timeout=max(1, int(timeout_seconds)) * 1000,
            )
            return True
        except (PlaywrightTimeoutError, PlaywrightError):
            return False

    async def wait_ready(self):
        """等待检索页完整脚本；脚本偶发未加载时进行有限的正常 reload 重试。"""
        total = max(15, int(self.cfg.get('site_ready_timeout_seconds', 90)))
        attempts = max(1, int(self.cfg.get('site_ready_reload_attempts', 3)))
        per_attempt = max(8, total // attempts)
        last_diag = {}

        for attempt in range(1, attempts + 1):
            try:
                await self.page.wait_for_function(
                    "window.jQuery && $.WebSite && typeof cipher === 'function'",
                    timeout=per_attempt * 1000,
                )
                if attempt > 1:
                    print(f"[browser] 检索页脚本第 {attempt} 次尝试已就绪。")
                return
            except (PlaywrightTimeoutError, PlaywrightError):
                last_diag = await self._page_diag()
                url = str(last_diag.get('url') or '')
                if 'noauth.html' in url:
                    raise FatalAccessRestriction("当前账户/实名权限不足（noauth）")
                if 'blackip.html' in url:
                    raise FatalAccessRestriction("访问被站点限制（blackip）；不会规避限制")

                if attempt < attempts:
                    print(
                        f"[browser] 检索页脚本尚未就绪，自动刷新后重试 "
                        f"({attempt}/{attempts})；url={url} title={last_diag.get('title','')}"
                    )
                    try:
                        await self.page.reload(wait_until="domcontentloaded", timeout=90000)
                    except PlaywrightError as e:
                        if "ERR_ABORTED" not in str(e):
                            print(f"[browser] 刷新检索页失败，将继续下一次检测：{e}")
                    await asyncio.sleep(1.0)

        raise RuntimeError(
            "裁判文书网检索页已打开，但站点脚本未完成初始化（缺少 jQuery/WebSite/cipher）。"
            f" 当前状态={last_diag}。可查看已打开的 Chrome 是否停在登录、验证码或访问提示页面；"
            "程序没有尝试绕过站点验证。"
        )

    async def user_info(self):
        try:
            return await self.page.evaluate("() => { try { return $.WebSite.getUserInfo ? $.WebSite.getUserInfo() : null } catch(e) { return null } }")
        except PlaywrightError:
            return None

    @staticmethod
    def _is_logged_in(info):
        if not isinstance(info, dict):
            return False
        # 当前裁判文书网 currentUser 实际返回可只有 userName；
        # 老版本页面也可能带 userId。二者任一能证明已登录即可。
        user_id = str(info.get('userId') or '').strip()
        user_name = str(info.get('userName') or '').strip()
        if user_id and user_id != 'anonymousUser':
            return True
        return bool(user_name)

    async def _wait_user_info(self, attempts: int = 5, delay: float = 1.0):
        last = None
        for _ in range(attempts):
            last = await self.user_info()
            if self._is_logged_in(last):
                return last
            await asyncio.sleep(delay)
        return last

    async def ensure_login(self):
        # 先只等 currentUser 所需的基础脚本；不要因为 cipher() 偶发慢加载而误判登录失败。
        await self._wait_base_ready(timeout_seconds=min(20, int(self.cfg.get('site_ready_timeout_seconds', 90))))
        info = await self._wait_user_info(attempts=3, delay=0.8)
        if self._is_logged_in(info):
            # 已登录时再把检索页完整脚本拉起来；必要时 wait_ready 会正常 reload 重试。
            await self.wait_ready()
            return info

        print("\n尚未检测到裁判文书网登录态。")
        print("程序会尝试打开正常登录页；如果网站中止自动导航，请直接在已打开的 Chrome 窗口里手工点击登录或打开登录页。")
        await self._goto(self.cfg['login_url'], tolerate_aborted=True)

        print("\n请在浏览器里正常登录，并手工完成网站验证码。")
        print("登录成功后，只要浏览器已经回到 wenshu.court.gov.cn 页面即可。")
        input("完成后回到 PyCharm 控制台，按 Enter 继续... ")

        # 无论登录页最后停在哪个 URL，都回到新的检索页做实际登录态验证。
        await self.open_search_page(require_ready=False)
        await self._wait_base_ready(timeout_seconds=20)
        info = await self._wait_user_info(attempts=6, delay=1.0)
        if not self._is_logged_in(info):
            safe_keys = sorted(info.keys()) if isinstance(info, dict) else []
            diag = await self._page_diag()
            raise RuntimeError(
                "仍未检测到有效登录态。浏览器窗口会保存独立 Profile；请重新运行，"
                "确认已在该窗口内正常登录后再按 Enter。"
                f" currentUser返回字段={safe_keys} 页面状态={diag}"
            )
        await self.wait_ready()
        return info

    async def _site_get_data(self, cfg: str, param: dict):
        timeout = int(self.cfg.get('query_wait_timeout_seconds',600))
        # 不传 error callback：保留网站自身 -11 验证码弹窗与重试机制。
        js = r'''({cfg, param}) => new Promise((resolve) => {
          const requestParam = Object.assign({}, param || {});
          const pageId = $.WebSite.getParameter("pageId");
          if (pageId && !Object.prototype.hasOwnProperty.call(requestParam, "pageId")) {
            requestParam.pageId = pageId;
          }
          $.WebSite.getData({
            cfg: cfg,
            param: requestParam,
            async: true,
            readUrlParam: false,
            rollback: function(data){ resolve(data); }
          });
        })'''
        try:
            return await asyncio.wait_for(self.page.evaluate(js, {"cfg": cfg, "param": param}), timeout=timeout)
        except Exception as e:
            url = self.page.url
            if 'noauth.html' in url:
                raise FatalAccessRestriction("站点返回 -12：当前账户/实名权限不足") from e
            if 'blackip.html' in url:
                raise FatalAccessRestriction("站点返回 -14：访问被站点限制；不会规避限制") from e
            if isinstance(e, asyncio.TimeoutError):
                raise SiteCallTimeout("站点调用长时间未完成；如果浏览器有验证码，请正常完成后重试") from e
            raise

    @staticmethod
    def _extract_cprq(conditions: list[dict]) -> str | None:
        values = [
            str(item.get("value") or "").strip()
            for item in conditions or []
            if str(item.get("key") or "").strip() == "cprq"
        ]
        return values[0] if len(values) == 1 and values[0] else None

    async def _prepare_date_filter(self, conditions: list[dict], *, force: bool = False):
        """Mirror the site's own advanced-search date submit hook.

        The current wenshu page posts /api/fp/cprq immediately before queryDoc
        whenever a date range is submitted.  Replaying recursive date slices
        without that hook can lead to a successful query response whose
        queryItemList silently drops cprq.
        """
        raw = self._extract_cprq(conditions)
        if not raw or " TO " not in raw:
            return None
        if not force and getattr(self, "_prepared_cprq", None) == raw:
            return raw

        start_date, end_date = [x.strip() for x in raw.split(" TO ", 1)]
        js = r'''async ({startDate, endDate}) => {
          const body = new URLSearchParams({
            inputCprqStartVal: startDate,
            inputCprqEndVal: endDate,
            gjjsSubmit: '1'
          }).toString();
          const r = await fetch('/api/fp/cprq', {
            method: 'POST',
            credentials: 'include',
            headers: {
              'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
              'X-Requested-With': 'XMLHttpRequest'
            },
            body
          });
          return {ok: r.ok, status: r.status};
        }'''
        result = await self.page.evaluate(js, {"startDate": start_date, "endDate": end_date})
        if not isinstance(result, dict) or not result.get("ok"):
            status = result.get("status") if isinstance(result, dict) else None
            raise RuntimeError(f"裁判日期预提交失败：cprq={raw}, httpStatus={status}")
        self._prepared_cprq = raw
        return raw

    def invalidate_prepared_date(self, value: str | None = None):
        current = getattr(self, "_prepared_cprq", None)
        if value is None or current == value:
            self._prepared_cprq = None

    @staticmethod
    def _inject_wire_conditions(param: dict, conditions: list[dict]) -> dict:
        """Mirror only the HAR-proven compatibility field.

        The active filters are carried by queryCondition.  The supplied HAR
        shows cprqStart/cprqEnd can remain stale URL values while
        queryCondition.cprq changes.  Therefore date conditions must not be
        copied into top-level cprqStart/cprqEnd.

        Keep s17 mirrored at top level because the bank-party request carries
        it both top-level and inside queryCondition.
        """
        s17_values = [
            str(item.get("value") or "")
            for item in conditions or []
            if str(item.get("key") or "").strip() == "s17"
        ]
        if len(s17_values) == 1:
            param["s17"] = s17_values[0]
        return param

    async def query(self, conditions: list[dict], page_num: int, page_size: int, sort_fields: str):
        await self._prepare_date_filter(conditions)
        ciphertext = await self.page.evaluate("cipher()")
        param = {
            "sortFields": sort_fields,
            "ciphertext": ciphertext,
            "pageNum": page_num,
            "pageSize": page_size,
            "queryCondition": json.dumps(conditions, ensure_ascii=False)
        }
        self._inject_wire_conditions(param, conditions)
        data = await self._site_get_data("com.lawyee.judge.dc.parse.dto.SearchDataDsoDTO@queryDoc", param)
        return data or {}

    async def facet(self, conditions: list[dict], group_field: str):
        await self._prepare_date_filter(conditions)
        param = {
            "groupFields": group_field,
            "facetLimit": 1000,
            "queryCondition": json.dumps(conditions, ensure_ascii=False),
        }
        self._inject_wire_conditions(param, conditions)
        data = await self._site_get_data("com.lawyee.judge.dc.parse.dto.SearchDataDsoDTO@leftDataItem", param)
        if not isinstance(data, dict):
            return []
        return data.get(group_field) or []

    async def is_down(self, doc_id: str) -> dict:
        js = r'''async (docId) => {
          const encoded = encodeURIComponent(docId);
          const body = new URLSearchParams({docId: encoded}).toString();
          const r = await fetch('/down/isDown', {method:'POST', credentials:'include',
            headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'}, body});
          const t = await r.text();
          try { return JSON.parse(t); } catch(e) { return {code:-999, msg:t.slice(0,500), httpStatus:r.status}; }
        }'''
        return await self.page.evaluate(js, doc_id)

    async def download_one(self, doc_id: str, target: Path) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self.page.expect_download(timeout=90000) as di:
                await self.page.evaluate("d => { window.location.href='/down/one?docId='+encodeURIComponent(d); }", doc_id)
            dl = await di.value
            await dl.save_as(str(target))
            return dl.suggested_filename
        except PlaywrightTimeoutError as e:
            raise RuntimeError("等待官方 Word 下载超时") from e
