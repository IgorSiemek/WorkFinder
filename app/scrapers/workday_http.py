from __future__ import annotations

import os
from threading import BoundedSemaphore
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright

from app.scrapers.workday_limits import workday_access

_browser_slot = BoundedSemaphore(1)


class WorkdayHttp:
    """Use one public careers browser session when HTTP receives a challenge."""

    def __init__(self) -> None:
        self.playwright = None
        self.browser = None
        self.page = None
        self.origin = ""
        self.has_slot = False

    def __enter__(self) -> WorkdayHttp:
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            try:
                if self.playwright is not None:
                    self.playwright.stop()
            finally:
                if self.has_slot:
                    self.has_slot = False
                    _browser_slot.release()

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:  # noqa: ANN003
        if self.page is None:
            with workday_access(url):
                response = getattr(httpx, method.lower())(url, **kwargs)
                if getattr(response, "headers", {}).get("cf-mitigated") != "challenge":
                    if not getattr(response, "is_redirect", False):
                        response.raise_for_status()
                    return response
            # Reserve the browser before locking its host. Another scan on the same host
            # can be waiting for this slot while the current scan is still using it.
            _browser_slot.acquire()
            self.has_slot = True
        try:
            with workday_access(url):
                if self.page is None:
                    self.start_browser(url)
                response = self.browser_request(method, url, kwargs)
                if not response.is_redirect:
                    response.raise_for_status()
                return response
        except BrowserError as exc:
            raise httpx.RequestError("Workday careers browser request failed") from exc

    def start_browser(self, url: str) -> None:
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".myworkdayjobs.com")
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parts[:2] != ["wday", "cxs"]
            or len(parts) < 4
        ):
            raise ValueError("Unsupported Workday careers API URL")
        self.origin = f"https://{parsed.netloc}"
        search_url = self.origin + "/" + "/".join(parts[:4]) + "/jobs"
        self.playwright = sync_playwright().start()
        browser_env = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR")
            if key in os.environ
        }
        self.browser = self.playwright.chromium.launch(
            headless=True, chromium_sandbox=True, env=browser_env
        )
        context = self.browser.new_context(accept_downloads=False, service_workers="block")
        self.page = context.new_page()
        with self.page.expect_response(
            lambda response: response.url == search_url and response.request.method == "POST",
            timeout=30000,
        ) as initial_search:
            navigation = self.page.goto(
                f"{self.origin}/en-US/{parts[3]}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            if navigation is not None and navigation.status >= 400:
                httpx.Response(
                    navigation.status,
                    headers=navigation.headers,
                    request=httpx.Request("GET", navigation.url),
                ).raise_for_status()
        initial_search.value.body()

    def browser_request(self, method: str, url: str, options: dict) -> httpx.Response:
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            raise ValueError("Workday request must stay on the careers origin")
        result = self.page.evaluate(
            """async ({url, method, body, timeout}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeout);
                try {
                    const response = await fetch(url, {
                        method,
                        headers: {Accept: 'application/json', 'Content-Type': 'application/json'},
                        body: body === null ? undefined : JSON.stringify(body),
                        redirect: 'manual',
                        signal: controller.signal
                    });
                    return {
                        status: response.status,
                        headers: Object.fromEntries(response.headers),
                        text: await response.text()
                    };
                } finally {
                    clearTimeout(timer);
                }
            }""",
            {
                "url": url,
                "method": method.upper(),
                "body": options.get("json"),
                "timeout": options.get("timeout", 20) * 1000,
            },
        )
        if not result["status"]:
            raise httpx.RequestError("Unexpected Workday browser redirect")
        return httpx.Response(
            result["status"],
            headers={
                key: value
                for key, value in result["headers"].items()
                if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
            },
            text=result["text"],
            request=httpx.Request(method.upper(), url),
        )
