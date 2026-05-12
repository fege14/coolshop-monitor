import os
import pathlib
import random
import signal
import sys
import time

import requests
from playwright.sync_api import sync_playwright

_BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}
_JITTER_SECONDS = 3
# Long-running Playwright sessions degrade — recycle the browser every 30min.
_BROWSER_RECYCLE_SECONDS = 30 * 60

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

# Seconds to wait between full sweeps over all URLs.
SWEEP_PAUSE = int(os.environ.get("SWEEP_PAUSE", "10"))

# Phrases that indicate the product can be purchased right now.
IN_STOCK_PHRASES = ["Læg i kurv", "Afhent i butik"]
# Phrase that indicates the product is not currently buyable.
OUT_OF_STOCK_PHRASES = ["Se populære produkter"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def load_urls():
    path = pathlib.Path(__file__).with_name("urls.txt")
    urls = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify(title, message, url=None, priority="high", tags="bell,shopping_cart"):
    if not NTFY_TOPIC:
        log(f"NOTIFY (dry-run) | {title} | {message}")
        return
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
        "Tags": tags,
    }
    if url:
        headers["Click"] = url
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
    except Exception as e:
        log(f"notify error: {e}")


def dismiss_consent(page):
    for sel in (
        "button:has-text('Accepter alle')",
        "button:has-text('Tillad alle')",
        "button:has-text('Accepter')",
        "#coiPage-1 .coi-banner__accept",
    ):
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=2000)
                return True
        except Exception:
            pass
    return False


_CHECK_STOCK_JS = """
([inStock, outOfStock]) => {
    const text = document.body ? document.body.innerText : '';
    const hasIn = inStock.some(p => text.includes(p));
    const hasOut = outOfStock.some(p => text.includes(p));
    return { hasIn, hasOut };
}
"""


def read_stock(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    dismiss_consent(page)
    page.wait_for_function(
        "([inStock, outOfStock]) => {"
        "  const text = document.body ? document.body.innerText : '';"
        "  return inStock.some(p => text.includes(p)) || outOfStock.some(p => text.includes(p));"
        "}",
        arg=[IN_STOCK_PHRASES, OUT_OF_STOCK_PHRASES],
        timeout=30_000,
    )
    flags = page.evaluate(_CHECK_STOCK_JS, [IN_STOCK_PHRASES, OUT_OF_STOCK_PHRASES])
    title = (page.title() or "").strip()
    if flags["hasIn"]:
        return "in_stock", title
    if flags["hasOut"]:
        return "out_of_stock", title
    return "unknown", title


def main():
    urls = load_urls()
    if not urls:
        log("ERROR: urls.txt is empty")
        return 1
    if not NTFY_TOPIC:
        log("WARN: NTFY_TOPIC not set — running in dry-run mode")

    last = {u: None for u in urls}
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    def _route(route):
        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()

    with sync_playwright() as pw:
        def _new_browser():
            br = pw.chromium.launch(headless=True)
            c = br.new_context(
                user_agent=UA,
                locale="da-DK",
                viewport={"width": 1366, "height": 900},
            )
            p = c.new_page()
            p.route("**/*", _route)
            return br, c, p

        browser, ctx, page = _new_browser()
        browser_started = time.time()

        log(f"started | urls={len(urls)} | sweep_pause={SWEEP_PAUSE}s")
        notify(
            "Coolshop monitor started",
            f"Watching {len(urls)} products",
            priority="low",
            tags="bell",
        )

        while not stop["flag"]:
            if time.time() - browser_started > _BROWSER_RECYCLE_SECONDS:
                log("recycling browser")
                try:
                    browser.close()
                except Exception as e:
                    log(f"browser close error: {e}")
                browser, ctx, page = _new_browser()
                browser_started = time.time()

            for url in urls:
                if stop["flag"]:
                    break
                try:
                    status, title = read_stock(page, url)
                except Exception as e:
                    log(f"error | {url} | {e}")
                    continue

                label = title or url
                prev = last[url]
                if prev is None:
                    log(f"baseline {status} | {label}")
                elif prev != status:
                    log(f"{prev} -> {status} | {label}")
                    if status == "in_stock":
                        notify(
                            f"IN STOCK: {title or 'Coolshop product'}",
                            f"{title}\n{url}" if title else url,
                            url=url,
                        )
                else:
                    log(f"{status} | {label}")
                last[url] = status

            if stop["flag"]:
                break
            time.sleep(SWEEP_PAUSE + random.uniform(-_JITTER_SECONDS, _JITTER_SECONDS))

        try:
            browser.close()
        except Exception:
            pass
    log("shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
