import json
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

# Status strings persisted in state.json:
#   "in_stock"          — "Læg i kurv" present → truly buyable online
#   "reserve_only:N"    — "Reservér i butik" CTA, N store locations claim
#                          stock. Not buyable online; clicking through often
#                          reveals "IKKE PÅ LAGER" even when N>0, so this is
#                          a soft signal — track N changes.
#   "out_of_stock"      — "Se populære produkter" placeholder shown
#   "unknown"           — none of the above matched
#
# "Afhent i butik" (pickup-in-store) is intentionally NOT an in_stock signal
# — it shows as a delivery option on reserve-only pages too, which caused
# false-positive in_stock alerts on ETB/blister when they flipped to
# reserve-only mode.
RESERVE_PHRASE = "Reservér i butik"
# Captures the location count from "På lager ved 2 lokationer" /
# "På lager ved 1 lokation".
RESERVE_COUNT_REGEX = r"På lager ved (\d+) lokation"
IN_STOCK_PHRASES = ["Læg i kurv"]
OUT_OF_STOCK_PHRASES = ["Se populære produkter"]

# Persisted across process restarts. Without this, every fresh boot (e.g.
# the 5h cron tick that kills the prior run via cancel-in-progress) would
# silently baseline whatever's on the page — any URL that flipped to
# in_stock during the cancel→restart gap would be absorbed into the new
# baseline and never trigger a notification.
STATE_FILE = pathlib.Path(__file__).resolve().parent / "state.json"

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


def load_state(urls):
    last = {u: None for u in urls}
    if not STATE_FILE.exists():
        return last
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"state load failed, starting fresh: {e}")
        return last
    for u in urls:
        s = raw.get("last", {}).get(u)
        if s is not None:
            last[u] = s
    return last


def save_state(last):
    try:
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"last": last}), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        log(f"state save failed: {e}")


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


# Priority order matters: a reserve-only page often contains delivery-option
# text that looks like an in-stock signal elsewhere on the page. Checking
# RESERVE_PHRASE first means a reserve-only CTA wins over stray matches.
_CHECK_STOCK_JS = """
([reservePhrase, reserveRegex, inStock, outOfStock]) => {
    const text = document.body ? document.body.innerText : '';
    if (text.includes(reservePhrase)) {
        const m = text.match(new RegExp(reserveRegex));
        const n = m ? (parseInt(m[1], 10) || 0) : 0;
        return { kind: 'reserve_only', count: n };
    }
    if (inStock.some(p => text.includes(p))) return { kind: 'in_stock', count: 0 };
    if (outOfStock.some(p => text.includes(p))) return { kind: 'out_of_stock', count: 0 };
    return { kind: 'unknown', count: 0 };
}
"""


def parse_status(status):
    """Decode persisted status string → (kind, count). count only meaningful
    when kind == 'reserve_only'."""
    if status and status.startswith("reserve_only:"):
        try:
            return "reserve_only", int(status.split(":", 1)[1])
        except (ValueError, IndexError):
            return "reserve_only", 0
    return status or "unknown", 0


def read_stock(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    dismiss_consent(page)
    page.wait_for_function(
        "([reservePhrase, inStock, outOfStock]) => {"
        "  const text = document.body ? document.body.innerText : '';"
        "  return text.includes(reservePhrase)"
        "    || inStock.some(p => text.includes(p))"
        "    || outOfStock.some(p => text.includes(p));"
        "}",
        arg=[RESERVE_PHRASE, IN_STOCK_PHRASES, OUT_OF_STOCK_PHRASES],
        timeout=30_000,
    )
    res = page.evaluate(
        _CHECK_STOCK_JS,
        [RESERVE_PHRASE, RESERVE_COUNT_REGEX, IN_STOCK_PHRASES, OUT_OF_STOCK_PHRASES],
    )
    title = (page.title() or "").strip()
    kind = res["kind"]
    if kind == "reserve_only":
        return f"reserve_only:{res['count']}", title
    return kind, title


def main():
    urls = load_urls()
    if not urls:
        log("ERROR: urls.txt is empty")
        return 1
    if not NTFY_TOPIC:
        log("WARN: NTFY_TOPIC not set — running in dry-run mode")

    last = load_state(urls)
    seeded = sum(1 for v in last.values() if v is not None)
    if seeded:
        log(f"loaded state from {STATE_FILE.name} | {seeded}/{len(urls)} URLs")
    else:
        log(f"no prior state at {STATE_FILE.name} — first poll will baseline silently")

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

    log(f"started | urls={len(urls)} | sweep_pause={SWEEP_PAUSE}s")
    notify(
        "Coolshop monitor started",
        f"Watching {len(urls)} products",
        priority="low",
        tags="bell",
    )

    session_attempt = 0
    while not stop["flag"]:
        session_attempt += 1
        try:
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
                            prev_kind, prev_n = parse_status(prev)
                            cur_kind, cur_n = parse_status(status)
                            # Notify on:
                            #   • any transition INTO in_stock (real buyability — high prio)
                            #   • out_of_stock → reserve_only:* (first store sees stock)
                            #   • reserve_only:N → reserve_only:M where M > N (more stores)
                            # Reserve-only count DECREASING is noise (someone
                            # reserved/depleted locally) — log but don't ping.
                            if cur_kind == "in_stock":
                                notify(
                                    f"IN STOCK: {title or 'Coolshop product'}",
                                    f"{title}\n{url}" if title else url,
                                    url=url,
                                )
                            elif cur_kind == "reserve_only" and prev_kind == "out_of_stock":
                                notify(
                                    f"Reservable ({cur_n} stores): {title or 'Coolshop product'}",
                                    f"{title}\n{url}" if title else url,
                                    url=url,
                                    priority="default",
                                )
                            elif (
                                cur_kind == "reserve_only"
                                and prev_kind == "reserve_only"
                                and cur_n > prev_n
                            ):
                                notify(
                                    f"More stores ({prev_n}→{cur_n}): {title or 'Coolshop product'}",
                                    f"{title}\n{url}" if title else url,
                                    url=url,
                                    priority="default",
                                )
                        else:
                            log(f"{status} | {label}")
                        last[url] = status
                        save_state(last)

                    if stop["flag"]:
                        break
                    time.sleep(SWEEP_PAUSE + random.uniform(-_JITTER_SECONDS, _JITTER_SECONDS))

                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            if stop["flag"]:
                break
            log(f"session #{session_attempt} crashed, restarting in 10s: {type(e).__name__}: {e}")
            time.sleep(10)
    log("shutdown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
