from __future__ import annotations
import asyncio, json, uuid, hashlib, os, getpass, sys
from pathlib import Path
from datetime import datetime
from urllib.parse import quote, urlparse, parse_qs, urlencode
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError, Error as PlaywrightError

class FatalAccessRestriction(RuntimeError): pass
class SiteCallTimeout(RuntimeError): pass

class WenshuBrowser:
    def __init__(self, cfg: dict, project_root: Path, debug_log_path: Path | None = None):
        self.cfg = cfg
        self.project_root = project_root
        self.pw = self.context = self.page = None
        self.debug_log_path = Path(debug_log_path) if debug_log_path else None
        self.debug_run_id = uuid.uuid4().hex
        self._date_context_key = None
        self._date_native_only_key = None
        self._date_context_data = None

    def _debug_append(self, event_type: str, payload: dict):
        if not self.debug_log_path:
            return
        try:
            self.debug_log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "recorded_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "run_id": self.debug_run_id,
                "event_type": event_type,
                "payload": payload,
            }
            with self.debug_log_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                fp.flush()
        except Exception:
            # Diagnostic logging must never break collection.
            pass

    @staticmethod
    def _safe_form(post_data: str | None) -> dict:
        parsed = parse_qs(post_data or "", keep_blank_values=True)
        out = {}
        for key, values in parsed.items():
            value = values[-1] if values else ""
            if key in ("ciphertext", "__RequestVerificationToken"):
                raw = str(value)
                out[key] = {
                    "length": len(raw),
                    "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                }
            else:
                out[key] = value
        return out

    def _is_query_doc_request(self, request) -> bool:
        try:
            return (
                "/website/parse/rest.q4w" in request.url
                and "queryDoc" in (request.post_data or "")
            )
        except Exception:
            return False

    def _on_request(self, request):
        if not self._is_query_doc_request(request):
            return
        try:
            headers = {str(k).lower(): str(v) for k, v in (request.headers or {}).items()}
            self._debug_append("query_http_request", {
                "method": request.method,
                "url": request.url,
                "page_url": self.page.url if self.page else "",
                "headers": {
                    "content-type": headers.get("content-type"),
                    "origin": headers.get("origin"),
                    "referer": headers.get("referer"),
                    "user-agent": headers.get("user-agent"),
                },
                "form": self._safe_form(request.post_data),
            })
        except Exception as e:
            self._debug_append("query_http_request_log_error", {"error": str(e)})

    def _on_response(self, response):
        request = response.request
        if not self._is_query_doc_request(request):
            return
        try:
            self._debug_append("query_http_response", {
                "url": response.url,
                "status": response.status,
                "page_url": self.page.url if self.page else "",
                "post_sha256": hashlib.sha256(
                    (request.post_data or "").encode("utf-8")
                ).hexdigest(),
            })
        except Exception as e:
            self._debug_append("query_http_response_log_error", {"error": str(e)})

    def _log_decoded_query(self, param: dict, data: dict):
        qp = (data or {}).get("queryParams") or {}
        qr = (data or {}).get("queryResult") or {}
        safe_param = dict(param)
        for key in ("ciphertext", "__RequestVerificationToken"):
            if key in safe_param:
                raw = str(safe_param[key])
                safe_param[key] = {
                    "length": len(raw),
                    "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                }
        self._debug_append("query_decoded_result", {
            "page_url": self.page.url if self.page else "",
            "requested_param": safe_param,
            "backend_queryItemList": qp.get("queryItemList"),
            "resultCount": qr.get("resultCount"),
            "response_keys": sorted((data or {}).keys()) if isinstance(data, dict) else [],
        })

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
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
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
        self._date_context_key = None
        self._date_native_only_key = None
        self._date_context_data = None
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

    def _read_login_credentials(self) -> tuple[str, str]:
        """Read credentials without storing secrets in the repository."""
        username_env = str(self.cfg.get('login_username_env') or 'WENSHU_USERNAME').strip()
        password_env = str(self.cfg.get('login_password_env') or 'WENSHU_PASSWORD').strip()

        username = str(os.environ.get(username_env) or '').strip()
        password = str(os.environ.get(password_env) or '')

        if not username:
            username = input("裁判文书网账号/手机号: ").strip()
        if not password:
            if not sys.stdin.isatty():
                raise RuntimeError(
                    f"当前控制台无法安全隐藏密码输入。请在运行环境中设置 {password_env}，"
                    f"例如 PyCharm Run Configuration -> Environment variables 中配置 "
                    f"{password_env}=你的密码；程序不会在非 TTY 控制台回显读取密码。"
                )
            password = getpass.getpass("裁判文书网密码: ")

        if not username or not password:
            raise RuntimeError(
                f"缺少登录凭据。可设置环境变量 {username_env} / {password_env}，"
                "或在支持隐藏输入的终端中运行。"
            )
        return username, password

    async def _wait_account_login_frame(self, timeout_seconds: int = 60):
        """Wait for the official unified-account login iframe proven by HAR."""
        deadline = asyncio.get_running_loop().time() + max(1, int(timeout_seconds))
        while asyncio.get_running_loop().time() < deadline:
            for frame in self.page.frames:
                try:
                    frame_url = str(frame.url or '')
                    if 'account.court.gov.cn' not in frame_url:
                        continue
                    # The HAR first shows a transient bare /app iframe, then
                    # the real OAuth-bound /app?back_url=... login frame.
                    if 'back_url=' not in frame_url and '/oauth/authorize' not in frame_url:
                        continue
                    user = frame.locator('input[name="username"]').first
                    pwd = frame.locator('input[name="password"]').first
                    if await user.count() and await pwd.count():
                        return frame
                except PlaywrightError:
                    continue
            await asyncio.sleep(0.5)
        raise SiteCallTimeout(
            "统一账号登录页未在预期时间内加载。请检查浏览器是否停在登录页、"
            "网络是否可访问 account.court.gov.cn。"
        )

    async def _login_error_text(self, frame) -> str:
        try:
            node = frame.locator('.login-error-tips').first
            if not await node.count():
                return ''
            text = str(await node.text_content() or '').strip()
            if text and await node.is_visible():
                return text
        except PlaywrightError:
            pass
        return ''

    async def _wait_login_callback(self, frame, timeout_seconds: int) -> None:
        """Wait until the official OAuth iframe returns control to wenshu."""
        deadline = asyncio.get_running_loop().time() + max(1, int(timeout_seconds))
        last_error = ''
        while asyncio.get_running_loop().time() < deadline:
            url = str(self.page.url or '')
            if 'wenshu.court.gov.cn' in url and '181010CARHS5BS3C' not in url:
                return

            # Wrong credentials are surfaced by the official account page.
            if 'account.court.gov.cn' in str(getattr(frame, 'url', '') or ''):
                err = await self._login_error_text(frame)
                if err:
                    last_error = err

            await asyncio.sleep(1.0)

        if last_error:
            raise RuntimeError(f"统一账号登录未成功：{last_error}")
        raise SiteCallTimeout(
            "等待登录/验证码完成超时。程序没有绕过验证码；"
            "请在已打开的浏览器中完成官方验证后重试。"
        )

    async def _auto_login_via_official_page(self):
        """Autofill the official login UI; captcha remains a human action.

        HAR evidence shows the login page embeds account.court.gov.cn in an
        iframe.  That page performs password encryption, TianAi captcha
        validation and /api/login itself, then OAuth redirects back to Wenshu.
        We therefore never replay the encrypted login API directly.
        """
        await self._goto(self.cfg['login_url'], tolerate_aborted=True)
        frame = await self._wait_account_login_frame(
            timeout_seconds=int(self.cfg.get('login_page_timeout_seconds', 60))
        )

        username, password = self._read_login_credentials()
        user_input = frame.locator('input[name="username"]').first
        password_input = frame.locator('input[name="password"]').first
        submit = frame.locator('[data-action="login-submit"]').first

        await user_input.fill(username)
        await password_input.fill(password)
        if not await submit.count():
            raise RuntimeError("统一账号登录按钮未找到，官网登录页结构可能已变化。")

        print("\n账号和密码已自动填入官网登录页。")
        print("程序将点击登录；如出现官方验证码，请在浏览器中手工完成。")
        print("验证码成功后程序会自动检测 OAuth 回跳，不需要再回控制台按 Enter。")
        await submit.click()

        await self._wait_login_callback(
            frame,
            timeout_seconds=int(self.cfg.get('login_wait_timeout_seconds', 600)),
        )

    async def ensure_login(self):
        # 先只等 currentUser 所需的基础脚本；不要因为 cipher() 偶发慢加载而误判登录失败。
        await self._wait_base_ready(timeout_seconds=min(20, int(self.cfg.get('site_ready_timeout_seconds', 90))))
        info = await self._wait_user_info(attempts=3, delay=0.8)
        if self._is_logged_in(info):
            # 已登录时再把检索页完整脚本拉起来；必要时 wait_ready 会正常 reload 重试。
            await self.wait_ready()
            return info

        print("\n尚未检测到裁判文书网登录态。")

        if bool(self.cfg.get('auto_login', True)):
            await self._auto_login_via_official_page()
        else:
            print("自动填充登录已关闭，请在已打开的浏览器中正常登录并完成人工验证码。")
            await self._goto(self.cfg['login_url'], tolerate_aborted=True)
            input("登录完成后回到控制台，按 Enter 继续... ")

        # OAuth 成功后重新打开检索页，用 Wenshu 自己的 currentUser 做最终确认。
        await self.open_search_page(require_ready=False)
        await self._wait_base_ready(timeout_seconds=20)
        info = await self._wait_user_info(attempts=8, delay=1.0)
        if not self._is_logged_in(info):
            safe_keys = sorted(info.keys()) if isinstance(info, dict) else []
            diag = await self._page_diag()
            raise RuntimeError(
                "统一账号流程结束，但裁判文书网仍未检测到有效登录态。"
                f" currentUser返回字段={safe_keys} 页面状态={diag}"
            )
        await self.wait_ready()
        return info

    async def _site_get_data(self, cfg: str, param: dict):
        timeout = int(self.cfg.get('query_wait_timeout_seconds',600))
        # 不传 error callback：保留网站自身 -11 验证码弹窗与重试机制。
        js = r'''({cfg, param}) => new Promise((resolve) => {
          $.WebSite.getData({
            cfg: cfg,
            param: param,
            async: true,
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
    def _date_context_value(conditions: list[dict]) -> str | None:
        values = [
            str(item.get("value") or "").strip()
            for item in conditions or []
            if str(item.get("key") or "").strip() == "cprq"
        ]
        return values[0] if len(values) == 1 and values[0] else None

    @staticmethod
    def _date_context_key_for(conditions: list[dict]) -> str | None:
        raw = WenshuBrowser._date_context_value(conditions)
        if not raw:
            return None
        return json.dumps(conditions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    async def _ensure_date_page_context(self, conditions: list[dict]):
        """Initialize a new date slice through the page's own onload path.

        Current website source restores URL conditions with
        addParams1545035259000($.WebSite.getParameter()) and then calls
        loadData().  A synthetic queryDoc sent after an unfiltered page load can
        receive HTTP 200 while cprq is silently omitted from queryItemList.
        """
        raw = self._date_context_value(conditions)
        key = self._date_context_key_for(conditions)
        if not raw or not key:
            return
        if self._date_context_key == key:
            return
        if " TO " not in raw:
            raise ValueError(f"非法裁判日期条件: {raw}")

        start_date, end_date = [x.strip() for x in raw.split(" TO ", 1)]
        page_id = uuid.uuid4().hex
        query_pairs = [
            ("pageId", page_id),
            ("cprqStart", start_date),
            ("cprqEnd", end_date),
        ]
        for item in conditions or []:
            k = str(item.get("key") or "").strip()
            if not k or k == "cprq":
                continue
            query_pairs.append((k, str(item.get("value") or "")))
        url = f"{self.cfg['search_url']}?{urlencode(query_pairs)}"

        self._debug_append("date_context_navigation", {
            "page_id": page_id,
            "cprq": raw,
            "url": url,
            "conditions": conditions,
        })
        await self._goto(url, tolerate_aborted=True)
        await self.wait_ready()

        timeout_ms = max(1, int(self.cfg.get("query_wait_timeout_seconds", 600))) * 1000
        expected = {"startDate": start_date, "endDate": end_date}
        js = r'''({startDate, endDate}) => {
          try {
            const d = $.WebSite.getModuleData('1545184311000');
            const items = (((d || {}).queryParams || {}).queryItemList || []);
            const norm = v => String(v || '').replace(/\\-/g, '-');
            const hasStart = items.some(x =>
              String(x.id || x.key || '') === 's31' &&
              norm(x.value) === startDate &&
              ['GREATER','GT','>'].includes(String(x.oper || '').toUpperCase())
            );
            const hasEnd = items.some(x =>
              String(x.id || x.key || '') === 's31' &&
              norm(x.value) === endDate &&
              ['LESS','LT','<'].includes(String(x.oper || '').toUpperCase())
            );
            return hasStart && hasEnd;
          } catch (e) {
            return false;
          }
        }'''
        try:
            await self.page.wait_for_function(js, arg=expected, timeout=timeout_ms)
        except (PlaywrightTimeoutError, PlaywrightError) as e:
            self._date_context_key = None
            raise SiteCallTimeout(
                f"页面按官方 URL 初始化后仍未形成日期条件: cprq={raw}"
            ) from e

        data = await self.page.evaluate(
            "() => $.WebSite.getModuleData('1545184311000')"
        )
        qp = (data or {}).get("queryParams") or {}
        qr = (data or {}).get("queryResult") or {}
        self._debug_append("date_context_ready", {
            "page_url": self.page.url,
            "cprq": raw,
            "backend_queryItemList": qp.get("queryItemList"),
            "resultCount": qr.get("resultCount"),
        })
        self._date_context_key = key
        self._date_native_only_key = None
        self._date_context_data = data

    @staticmethod
    def _response_has_date(data: dict, raw: str) -> bool:
        if not raw or " TO " not in raw:
            return True
        start_date, end_date = [x.strip() for x in raw.split(" TO ", 1)]
        items = (((data or {}).get("queryParams") or {}).get("queryItemList") or [])
        norm = lambda v: str(v or "").replace("\\-", "-")
        has_start = any(
            str(x.get("id") or x.get("key") or "") == "s31"
            and norm(x.get("value")) == start_date
            and str(x.get("oper") or "").upper() in ("GREATER", "GT", ">")
            for x in items if isinstance(x, dict)
        )
        has_end = any(
            str(x.get("id") or x.get("key") or "") == "s31"
            and norm(x.get("value")) == end_date
            and str(x.get("oper") or "").upper() in ("LESS", "LT", "<")
            for x in items if isinstance(x, dict)
        )
        return has_start and has_end

    async def _native_query_current_date_context(self, page_num: int, page_size: int, sort_fields: str):
        """Use the exact list refresh path triggered by website sort/pagination UI."""
        timeout_ms = max(1, int(self.cfg.get("query_wait_timeout_seconds", 600))) * 1000
        payload = {
            "pageNum": int(page_num),
            "pageSize": int(page_size),
            "sortFields": str(sort_fields or "s51:desc"),
        }
        js = r'''({pageNum, pageSize, sortFields}) => {
          if (typeof loadData1545184311000 !== 'function') {
            return {ok:false, reason:'loadData1545184311000 missing'};
          }
          const $m = $('#_view_1545184311000');
          const parts = String(sortFields || 's51:desc').split(':', 2);
          const sortKey = parts[0] || 's51';
          const sortDir = (parts[1] || 'desc').toLowerCase();

          // Mirror the website's sort-click state before loadData().
          $m.find('.tool_PX').removeClass('tool_On tool_OnUp');
          const $sort = $m.find(".tool_PX[data-value='" + sortKey + "']").first();
          if (!$sort.length) {
            return {ok:false, reason:'sort control missing: ' + sortKey};
          }
          $sort.addClass(sortDir === 'asc' ? 'tool_OnUp' : 'tool_On');

          // The successful manual HAR uses the website's page-size selector.
          const $size = $m.find('select.pageSizeSelect').first();
          if (!$size.length) {
            return {ok:false, reason:'pageSizeSelect missing'};
          }
          $size.val(String(pageSize));

          // Avoid reading stale module data while the refresh is in flight.
          $('body').removeData('1545184311000');

          // This is the same argument shape used by initEvents when the user
          // clicks a sort control or page button.
          const postData = loadData1545184311000({
            searchMid: '1545035259000',
            pageNum: pageNum
          });
          return {ok: postData !== false};
        }'''
        async with self.page.expect_response(
            lambda r: self._is_query_doc_request(r.request),
            timeout=timeout_ms,
        ) as response_info:
            started = await self.page.evaluate(js, payload)
            if not isinstance(started, dict) or not started.get("ok"):
                reason = started.get("reason") if isinstance(started, dict) else started
                raise RuntimeError(f"网页原生日期查询启动失败: {reason}")
        await response_info.value
        await self.page.wait_for_function(
            r'''() => {
              try {
                const d = $.WebSite.getModuleData('1545184311000');
                return !!(d && d.queryParams && d.queryResult);
              } catch(e) { return false; }
            }''',
            timeout=timeout_ms,
        )
        data = await self.page.evaluate(
            "() => $.WebSite.getModuleData('1545184311000')"
        )
        data = data or {}
        self._log_decoded_query({
            "mode": "native-date-context",
            "pageNum": page_num,
            "pageSize": page_size,
            "sortFields": sort_fields,
        }, data)
        return data

    def invalidate_date_context(self, value: str | None = None):
        if value is None:
            self._date_context_key = None
            self._date_native_only_key = None
            self._date_context_data = None
            return
        # The cached key contains the serialized cprq value; invalidate only
        # when it refers to the failed slice.
        if self._date_context_key and value in self._date_context_key:
            self._date_context_key = None
            self._date_native_only_key = None
            self._date_context_data = None

    @staticmethod
    def _inject_wire_conditions(param: dict, conditions: list[dict]) -> dict:
        """Mirror the current website request shape.

        Real HAR evidence:
        - s17 is sent both top-level and in queryCondition.
        - date UI sends cprqStart/cprqEnd top-level, while queryCondition keeps
          cprq=START TO END.
        """
        counts: dict[str, int] = {}
        for item in conditions or []:
            key = str(item.get("key") or "").strip()
            if key:
                counts[key] = counts.get(key, 0) + 1

        for item in conditions or []:
            key = str(item.get("key") or "").strip()
            if not key or counts.get(key) != 1:
                continue
            value = str(item.get("value") or "")
            if key == "cprq" and " TO " in value:
                start_date, end_date = value.split(" TO ", 1)
                param["cprqStart"] = start_date.strip()
                param["cprqEnd"] = end_date.strip()
            else:
                param[key] = value
        return param

    async def query(self, conditions: list[dict], page_num: int, page_size: int, sort_fields: str):
        raw_date = self._date_context_value(conditions)
        if raw_date:
            if int(page_size) != 5:
                self._debug_append("date_page_size_override", {
                    "requested_page_size": page_size,
                    "effective_page_size": 5,
                    "cprq": raw_date,
                })
                page_size = 5
            await self._ensure_date_page_context(conditions)
            initial = self._date_context_data or {}
            initial_count = ((initial.get("queryResult") or {}).get("resultCount"))
            try:
                initial_count = int(initial_count or 0)
            except (TypeError, ValueError):
                initial_count = 0
            if page_num == 1 and initial_count == 0:
                self._debug_append("date_native_empty_from_context", {
                    "cprq": raw_date,
                    "resultCount": 0,
                })
                return initial
            self._debug_append("date_native_query", {
                "cprq": raw_date,
                "pageNum": page_num,
                "pageSize": page_size,
                "sortFields": sort_fields,
            })
            for native_attempt in range(1, 3):
                try:
                    return await self._native_query_current_date_context(
                        page_num, page_size, sort_fields
                    )
                except RuntimeError as e:
                    message = str(e)
                    if not message.startswith("网页原生日期查询启动失败:"):
                        raise
                    if native_attempt >= 2:
                        raise
                    diag = await self._page_diag()
                    self._debug_append("date_native_context_recover", {
                        "cprq": raw_date,
                        "pageNum": page_num,
                        "pageSize": page_size,
                        "sortFields": sort_fields,
                        "reason": message,
                        "page_diag": diag,
                    })
                    print(
                        f"  [query-recover] 当前检索页模块失效，"
                        f"重建日期上下文并重试当前第 {page_num} 页: {message}"
                    )
                    self.invalidate_date_context(raw_date)
                    await self._ensure_date_page_context(conditions)
            raise RuntimeError("日期查询恢复循环异常退出")

        ciphertext = await self.page.evaluate("cipher()")
        param = {
            "sortFields": sort_fields,
            "ciphertext": ciphertext,
            "pageNum": page_num,
            "pageSize": page_size,
            "queryCondition": json.dumps(conditions, ensure_ascii=False, separators=(",", ":"))
        }
        self._inject_wire_conditions(param, conditions)
        data = await self._site_get_data("com.lawyee.judge.dc.parse.dto.SearchDataDsoDTO@queryDoc", param)
        data = data or {}
        self._log_decoded_query(param, data)
        return data

    async def facet(self, conditions: list[dict], group_field: str):
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
