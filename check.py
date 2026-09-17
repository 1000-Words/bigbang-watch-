#!/usr/bin/env python3
"""
One-shot BIGBANG HK Ticketing check, designed to run on a GitHub Actions schedule.

Each run: open the event page, click through the "I agree" screen, read the main status
button (currently "Sold Out"), and compare with the last run (state.json). Alerts via
ntfy when that button changes to anything else, or a waiting room appears.
Watches and notifies only; never buys.

Env:
  EVENT_URLS  - one or more HK Ticketing event URLs, separated by spaces or newlines
  NTFY_TOPIC  - ntfy.sh topic your phone is subscribed to
"""
import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

AGREE_RE = re.compile(r"^\s*(i\s*agree|agree|accept|confirm|同意|我同意|確認)\s*$", re.I)
SEAT_MAP_RE = re.compile(r"seat\s*map|座位表", re.I)
# Fallback: known labels for the main status button, matched only as the element's entire text
# (so the "[SOLD OUT]" in the presale description never counts).
CTA_LABELS = ["sold out", "buy now", "buy tickets", "book now", "coming soon", "on sale soon",
              "售罄", "已售罄", "已售完", "立即購買", "立即購票", "即將開售"]
QUEUE = ["waiting room", "you are in the queue", "排隊", "等候室", "system is busy", "系統繁忙"]
BLOCKED = ["verify you are human", "just a moment", "access denied", "checking your browser"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")
ALERT_COOLDOWN = 30 * 60        # don't repeat availability alerts within 30 min
PROBLEM_COOLDOWN = 6 * 60 * 60  # "watcher has a problem" alerts at most every 6 h
TICKET_REPEAT = 6               # ticket alerts are sent this many times, 10 s apart

# Reads the label of the button that sits next to "Seat map" in the event header.
CTA_JS = r"""
(labels) => {
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const els = [...document.querySelectorAll('body *')].filter(visible);

  const seat = els.find(el => /^(seat\s*map|座位表)$/i.test(clean(el.textContent)) &&
                              ![...el.children].some(c => /seat\s*map|座位表/i.test(c.textContent)));
  if (seat) {
    let node = seat.parentElement;
    for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
      const t = clean(node.innerText.replace(/seat\s*map|座位表/ig, ''));
      if (t) return t.length <= 40 ? t : null;
    }
  }
  for (const el of els) {
    const t = clean(el.textContent).toLowerCase();
    if (labels.includes(t)) return t;
  }
  return null;
}
"""


def push(title, msg, url=None, priority="urgent", repeat=1, gap=10):
    """Send an ntfy notification. repeat>1 sends it several times, gap seconds apart."""
    print(f"ALERT | {title}: {msg} (x{repeat})", flush=True)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC not set; skipping push")
        return
    headers = {"Title": title, "Priority": priority, "Tags": "ticket"}
    if url:
        headers["Click"] = url
    for i in range(repeat):
        body = msg if repeat == 1 else f"{msg} ({i + 1}/{repeat})"
        try:
            req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers)
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"ntfy push failed: {e}")
        if i < repeat - 1:
            time.sleep(gap)


# Scrolls every scrollable box on the page (i.e. the notice's text area) to the bottom
# and fires scroll events, which is what unlocks the greyed-out "I Agree" button.
SCROLL_JS = r"""
() => {
  for (const el of document.querySelectorAll('*')) {
    const s = getComputedStyle(el);
    if (el.scrollHeight > el.clientHeight + 5 && /(auto|scroll)/.test(s.overflowY)) {
      el.scrollTop = el.scrollHeight;
      el.dispatchEvent(new Event('scroll', { bubbles: true }));
    }
  }
}
"""


def _agree_enabled(btn):
    try:
        cls = (btn.get_attribute("class") or "").lower()
        return btn.is_enabled() and "disabled" not in cls
    except Exception:
        return False


def click_agree(page):
    """Get through the 'Ticket Purchase Notice' pop-up. Returns True if it clicked I Agree.

    The pop-up only covers the page; the status button underneath can be read either way,
    so a failure here is logged but doesn't stop the check.
    """
    btn = page.locator("button", has_text=AGREE_RE).first
    try:
        btn.wait_for(state="visible", timeout=10000)
    except Exception:
        btn = page.get_by_text(AGREE_RE).first
        try:
            btn.wait_for(state="visible", timeout=2000)
        except Exception:
            return False  # no pop-up this time

    # Scroll the notice to the bottom: JS scroll plus real mouse-wheel over the notice text.
    for _ in range(25):
        page.evaluate(SCROLL_JS)
        box = btn.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] / 2 - 300, box["y"] - 250)
            page.mouse.wheel(0, 2500)
        page.wait_for_timeout(300)
        if _agree_enabled(btn):
            break

    try:
        btn.click(timeout=4000)
        page.wait_for_timeout(2000)
        print("  scrolled the notice and clicked I Agree")
        return True
    except Exception as e:
        print(f"  couldn't click I Agree ({type(e).__name__}); reading the page underneath anyway")
        return False


def scan(page, url, idx):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    agreed = click_agree(page)

    detail_loaded = False
    for attempt in range(2):
        try:
            page.get_by_text(SEAT_MAP_RE).first.wait_for(state="visible", timeout=25000)
            detail_loaded = True
            break
        except Exception:
            if attempt == 0:
                # The agree screen may have sent us somewhere else; go back to the event.
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                agreed = click_agree(page) or agreed

    page.wait_for_timeout(1500)  # let the status button render its final label
    text = re.sub(r"\s+", " ", page.inner_text("body").lower())
    cta = page.evaluate(CTA_JS, CTA_LABELS) if detail_loaded else None

    DEBUG_DIR.mkdir(exist_ok=True)
    page.screenshot(path=str(DEBUG_DIR / f"page{idx}.png"), full_page=False)
    (DEBUG_DIR / f"page{idx}.txt").write_text(text, encoding="utf-8")

    return {
        "loaded": detail_loaded,
        "cta": cta.lower() if cta else None,
        "queue": any(m in text for m in QUEUE),
        "blocked": any(m in text for m in BLOCKED),
    }


def main():
    urls = os.environ.get("EVENT_URLS", "").split()
    if not urls:
        raise SystemExit("EVENT_URLS is empty. Add it under Settings > Secrets and variables > Actions > Variables.")

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    now = time.time()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="en-HK", viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        for idx, url in enumerate(urls):
            key = hashlib.sha1(url.encode()).hexdigest()[:10]
            prev = state.get(key, {})
            try:
                s = scan(page, url, idx)
            except Exception as e:
                print(f"check failed for {url}: {type(e).__name__}: {e}")
                continue
            print(f"{url}\n  loaded={s['loaded']} status-button={s['cta']!r} "
                  f"queue={s['queue']} blocked={s['blocked']}")

            def problem(msg):
                if now - prev.get("problem_alert", 0) > PROBLEM_COOLDOWN:
                    push("Watcher needs a look", msg, url, priority="default")
                    prev["problem_alert"] = now

            if s["blocked"]:
                problem("HK Ticketing is showing a bot check to GitHub's servers, so the watcher can't see the page.")
                state[key] = prev
                continue
            if s["queue"] and not prev.get("queue"):
                push("BIGBANG: waiting room appeared", "A queue showed up, which usually means a ticket release.", url)
                prev["last_alert"] = now
            prev["queue"] = s["queue"]

            if not s["loaded"] or not s["cta"]:
                if not s["queue"]:
                    problem("Couldn't read the event's status button (stuck on the agree screen or the layout changed). "
                            "Check the page-screenshots artifact.")
                state[key] = prev
                continue

            first_run = "cta" not in prev
            if first_run:
                push("BIGBANG watcher is live", f"Status button currently says: {s['cta'].title()}",
                     url, priority="default")
                if s["cta"] != "sold out":
                    push("BIGBANG tickets?", f"Status button says \"{s['cta'].title()}\", not Sold Out.", url, repeat=TICKET_REPEAT)
                    prev["last_alert"] = now
            elif s["cta"] != prev["cta"] and s["cta"] != "sold out":
                if now - prev.get("last_alert", 0) > ALERT_COOLDOWN:
                    push("BIGBANG tickets?", f"Status changed: \"{prev['cta'].title()}\" → \"{s['cta'].title()}\"", url,
                         repeat=TICKET_REPEAT)
                    prev["last_alert"] = now

            prev.update({"url": url, "cta": s["cta"]})
            state[key] = prev

        browser.close()

    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
