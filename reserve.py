"""One-shot BUAA venue-reservation helper.

It deliberately uses normal browser interactions, not undocumented APIs or
request flooding.  Credentials are read only from environment variables.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.async_api import Frame, Page, TimeoutError as PlaywrightTimeoutError, async_playwright

LOGIN_URL = (
    "https://d.buaa.edu.cn/https/77726476706e69737468656265737421"
    "e3e44ed225256951300d8db9d6562d/login?service="
    "https://cgyy.buaa.edu.cn/venue-server/sso/manageLogin"
)
VENUE_TITLE = "学院路校区-体育馆训练馆(副馆)"
TZ = ZoneInfo("Asia/Shanghai")


def env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def click_text(page: Page, text: str, *, exact: bool = False) -> None:
    """Click the first visible control containing text, with a clear failure."""
    control = page.get_by_role("button", name=text, exact=exact).or_(
        page.get_by_text(text, exact=exact)
    ).first
    await control.wait_for(state="visible", timeout=15_000)
    await control.click()


async def find_login_form(page: Page, timeout_seconds: float = 30) -> tuple[Frame, object, object]:
    """Find the form whether the identity portal renders it in an iframe or not."""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        for frame in page.frames:
            user = frame.locator("#unPassword")
            passwd = frame.locator("#pwPassword")
            if await user.count() and await passwd.count():
                if await user.is_visible() and await passwd.is_visible():
                    return frame, user, passwd
        await asyncio.sleep(0.25)
    raise PlaywrightTimeoutError(
        "在主页面及 iframe 中均未找到 #unPassword / #pwPassword。"
    )


async def login(page: Page, username: str, password: str) -> None:
    # `commit` prevents a slow resource on the portal from blocking form entry.
    await page.goto(LOGIN_URL, wait_until="commit")
    form_frame, user, passwd = await find_login_form(page)
    await user.fill(username)
    await passwd.fill(password)
    login_button = form_frame.get_by_role("button", name="登录").or_(
        form_frame.locator('input[type="submit"], button:has-text("登录")')
    ).first
    await login_button.click()
    # Do not insist on networkidle: the destination is a long-lived web app.
    await page.wait_for_load_state("domcontentloaded", timeout=30_000)


async def choose_venue_and_slot(page: Page) -> None:
    # First select the venue card by its stable HTML title, rather than its
    # visual position under the “热门场地” section.
    venue = page.locator(f'dt[title="{VENUE_TITLE}"]')
    print("123")
    await venue.wait_for(state="visible", timeout=15_000)
    await venue.click()
    print("127")

    # Select the card specifically assigned `order: 2` by the reservation UI.
    # Accept either browser serialization (`order: 2`) or minified (`order:2`).
    last_date = page.locator(
        '.date_box > div[style*="order: 2"], '
        '.date_box > div[style*="order:2"]'
    ).first
    print("125")
    await last_date.wait_for(state="visible", timeout=15_000)
    await last_date.click()
    print("126")
    # In the supplied table markup, the first cell is the court number, the
    # second is 07:00-08:00, and the third is 08:00-09:00.
    court_row = page.locator("table tbody tr").filter(
        has=page.get_by_text("17号", exact=True)
    ).first
    await court_row.wait_for(state="visible", timeout=15_000)
    target_slot = court_row.locator("td").nth(2).locator(
        ".reserveBlock.position.free"
    ).first
    if await target_slot.count() == 0:
        raise RuntimeError("17号场地在 08:00-09:00 时段不可预约。")
    await target_slot.click()

    # The agreement control is an iView checkbox. `check` handles its native
    # input state even when the visual checkbox is styled by its parent label.
    agreement = page.locator('input.ivu-checkbox-input').first
    await agreement.wait_for(state="attached", timeout=15_000)
    await agreement.check(force=True)


async def main() -> None:
    load_dotenv()
    username = os.environ.get("BUAA_USERNAME", "")
    password = os.environ.get("BUAA_PASSWORD", "")
    open_at_raw = os.environ.get("OPEN_AT", "")
    auto_submit = env_bool(os.environ.get("AUTO_SUBMIT", "false"))
    if not username or not password or not open_at_raw:
        raise SystemExit("请在 .env 中设置 BUAA_USERNAME、BUAA_PASSWORD 和 OPEN_AT。")

    open_at = datetime.fromisoformat(open_at_raw).astimezone(TZ)
    login_at = open_at - timedelta(minutes=2)
    delay = (login_at - datetime.now(TZ)).total_seconds()
    # if delay > 0:
    #     print(f"将在 {login_at:%F %T %Z} 打开登录页。")
    #     await asyncio.sleep(delay)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await login(page, username, password)
        await choose_venue_and_slot(page)
        if not auto_submit:
            print("已完成选场，程序停在提交前；请核对页面后手动提交。")
            # Keep the real browser open for manual review.  Ctrl+C ends this
            # helper after the user has submitted or decided not to reserve.
            await asyncio.Event().wait()
            return
        # This creates a real reservation. Keep it opt-in in .env.
        await click_text(page, "提交", exact=True)
        print("已点击提交；请在页面中核对预约结果。")
        await page.wait_for_timeout(5_000)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except PlaywrightTimeoutError as exc:
        raise SystemExit("页面元素未在规定时间出现，可能页面改版、未开放或需要人工验证。") from exc
