"""Page calls. A read is an observation. A click, type, or key is a new grant."""

import asyncio
from urllib.parse import urlsplit

VERBS = frozenset({"click", "type", "press"})
_FIELDS = {
    "browser_read": frozenset({"url"}),
    "browser_act": frozenset({"url", "verb", "target", "text"}),
}


def check_page(action: dict) -> dict:
    kind = action.get("type")
    params = action.get("params")
    allowed = _FIELDS.get(kind)
    if allowed is None or not isinstance(params, dict) or set(params) - allowed:
        raise ValueError("Page action has an unknown field")
    url = params.get("url")
    parts = urlsplit(url if isinstance(url, str) else "")
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or len(url) > 300):
        raise ValueError("Page URL must be a short https address")
    if kind == "browser_read":
        return {"url": url}
    verb = params.get("verb")
    target = params.get("target")
    if verb not in VERBS or not isinstance(target, str) or not target.strip() or len(target) > 80:
        raise ValueError("Page action needs a short target")
    text = params.get("text", "")
    if verb == "type":
        if not isinstance(text, str) or not text or len(text) > 200:
            raise ValueError("Page type needs short text")
    elif "text" in params:
        raise ValueError("Page action has an unknown field")
    return {"url": url, "verb": verb, "target": target, "text": text}


def host_allowed(url: str, hosts) -> bool:
    name = (urlsplit(url).hostname or "").lower()
    allowed = {item.strip().lower() for item in hosts if item and item.strip()}
    return any(name == item or name.endswith("." + item) for item in allowed)


class AllowingPage:
    """The allowlist is the only origin check. The inner page does not choose a host."""

    def __init__(self, inner, hosts):
        self.inner = inner
        self.hosts = hosts

    async def __call__(self, action):
        params = check_page(action)
        if not host_allowed(params["url"], self.hosts):
            raise ValueError("Page host is not allowed")
        return await self.inner(action)


class PlaywrightPage:
    """Empty Chromium. One call does one read or one act, then closes the tab. It does not replay."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None

    async def __call__(self, action):
        params = check_page(action)
        async with self._lock:
            page = await self._open()
            try:
                await page.goto(params["url"], wait_until="domcontentloaded", timeout=15000)
                if action.get("type") == "browser_act":
                    await _act(page, params)
                title = await page.title()
                excerpt = await page.inner_text("body")
            finally:
                await page.close()
        return {
            "url": params["url"],
            "title": " ".join(str(title).split())[:120],
            "excerpt": " ".join(str(excerpt).split())[:500],
        }

    async def _open(self):
        if self._browser is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as error:
                raise RuntimeError("Chromium library is not installed") from error
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=True)
        return await self._browser.new_page()

    async def close(self):
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._browser = None
            self._playwright = None


async def _act(page, params):
    target = params["target"]
    if params["verb"] == "click":
        await page.get_by_text(target, exact=True).click(timeout=8000)
    elif params["verb"] == "type":
        await page.get_by_label(target).fill(params["text"], timeout=8000)
    else:
        await page.keyboard.press(target)
