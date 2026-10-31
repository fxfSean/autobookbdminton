"""One-shot BUAA venue-reservation helper.

It deliberately uses normal browser interactions, not undocumented APIs or
request flooding.  Credentials are read only from environment variables.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.async_api import (
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

LOGIN_URL = (
    "https://d.buaa.edu.cn/https/77726476706e69737468656265737421"
    "e3e44ed225256951300d8db9d6562d/login?service="
    "https://cgyy.buaa.edu.cn/venue-server/sso/manageLogin"
)
VENUE_TITLE = "学院路校区-体育馆训练馆(副馆)"
VENUE_MAIN_TITLE = "学院路校区-体育馆主馆"
TZ = ZoneInfo("Asia/Shanghai")
VENUE = [20,21,22,23,24,17,18,19]
VENUE_MAIN = [1,2,3,4,5,6,7,8,9,10,11,12]

DAYS = [0,1,2]
ORDER_DAY = 2
# Zero-based time-slot order: 0 is the first time block after the court label.
ORDER_TIME = 1


def env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def click_text(page: Page, text: str, *, exact: bool = False) -> None:
    """Click the first visible control containing text, with a clear failure."""
    control = page.get_by_role("button", name=text, exact=exact).or_(
        page.get_by_text(text, exact=exact)
    ).first
    await control.wait_for(state="visible", timeout=15_000)
    await control.click()


def is_login_path(url: str) -> bool:
    """The portal can stay in front of the app (WebVPN path proxy), so only
    the path tells us whether we are still on the login page."""
    return urlparse(url).path.rstrip("/").endswith("/login")


async def find_login_form(
    page: Page, timeout_seconds: float = 30, *, required: bool = True
) -> tuple[Frame, object, object] | None:
    """Find the form whether the identity portal renders it in an iframe or not.

    With ``required=False`` this returns ``None`` instead of raising, which is
    how we tell "still on the login page" from "already signed in".
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        for frame in page.frames:
            try:
                user = frame.locator("#unPassword")
                passwd = frame.locator("#pwPassword")
                if await user.count() and await passwd.count():
                    if await user.is_visible() and await passwd.is_visible():
                        return frame, user, passwd
            except PlaywrightError:
                # The frame navigated or was detached while we probed it; the
                # next round will see the new document.
                continue
        await asyncio.sleep(0.25)
    if not required:
        return None
    raise PlaywrightTimeoutError(
        "在主页面及 iframe 中均未找到 #unPassword / #pwPassword。"
    )


async def wait_for_session_or_form(
    page: Page, timeout_seconds: float = 45.0
) -> tuple[Frame, object, object] | None:
    """Return the login form when a login is needed, ``None`` for a live session.

    Both signals are polled at the same time: the login form appearing, and the
    URL leaving ``/login``.  Polling them in sequence made a saved session look
    idle for the whole form timeout, because the form simply never shows up.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        form = await find_login_form(page, 0.25, required=False)
        if form is not None:
            return form
        if not is_login_path(page.url):
            return None
        await asyncio.sleep(0.2)

    raise PlaywrightTimeoutError(
        "登录页既没有出现账号密码表单，也没有离开登录地址。"
    )


async def wait_for_login_result(page: Page, timeout_seconds: float = 60.0) -> None:
    """Block until the login form has stayed away long enough to call it done.

    The portal proxies the whole site, so the URL keeps the portal host after a
    successful login.  The only reliable signal is the form disappearing, and
    we require it to stay gone for a couple of seconds so that a navigation in
    progress is not mistaken for success.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    missing_polls = 0
    while asyncio.get_running_loop().time() < deadline:
        form = await find_login_form(page, 0.3, required=False)
        if form is None:
            missing_polls += 1
            if missing_polls >= 4:
                try:
                    # Do not insist on networkidle: the destination is a
                    # long-lived web app.
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=30_000
                    )
                except PlaywrightTimeoutError:
                    pass
                return
        else:
            missing_polls = 0
        await asyncio.sleep(0.5)
    raise RuntimeError(
        "登录后仍停留在登录页：请检查账号密码，或页面是否要求验证码。"
    )


async def save_state(page: Page, state_path: str) -> None:
    """Persist cookies so the next run can skip the login (and its captcha)."""
    if not state_path:
        return
    target = Path(state_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    await page.context.storage_state(path=str(target))
    print(f"已保存登录状态到 {target}")


async def login(page: Page, username: str, password: str, state_path: str = "") -> None:
    # `commit` prevents a slow resource on the portal from blocking form entry.
    print("正在检查登录状态…")
    await page.goto(LOGIN_URL, wait_until="commit")
    form = await wait_for_session_or_form(page)

    if form is None:
        print("已复用保存的登录状态，跳过登录。")
        return

    form_frame, user, passwd = form
    await user.fill(username)
    await passwd.fill(password)

    # A captcha means a human has to finish this round. We fill in the
    # credentials first so only the code has to be typed in the browser.
    try:
        captcha = form_frame.locator('input[placeholder*="验证码"]').first
        human_needed = bool(await captcha.count()) and await captcha.is_visible()
    except PlaywrightError:
        # The frame navigated between the probe and the check; let the wait
        # below decide whether the login actually went through.
        human_needed = True

    if human_needed:
        print(
            "检测到验证码：账号和密码已填好，"
            "请在打开的浏览器窗口中输入验证码并点击“登录”…"
        )
    else:
        login_button = form_frame.get_by_role("button", name="登录").or_(
            form_frame.locator('input[type="submit"], button:has-text("登录")')
        ).first
        await login_button.click()

    await wait_for_login_result(page, 300.0 if human_needed else 60.0)
    print(f"登录完成，当前页面：{page.url}")
    await save_state(page, state_path)


async def wait_for_court_table(page: Page, timeout_seconds: float = 20):
    """Wait until the async table contains real court rows, not only its footer."""
    rows = page.locator(
        ".reserveTable #scrollTable.spaceTable > table > tbody.ivu-table-tbody > tr"
    )
    first_cells = page.locator(
        ".reserveTable #scrollTable.spaceTable > table "
        "> tbody.ivu-table-tbody > tr > td:first-child"
    )
    expected_labels = {f"{court_number}号" for court_number in VENUE}
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_labels: list[str] = []

    while asyncio.get_running_loop().time() < deadline:
        raw_labels = await first_cells.all_inner_texts()
        last_labels = ["".join(label.split()) for label in raw_labels]
        if expected_labels.intersection(last_labels):
            return rows
        await asyncio.sleep(0.25)

    raise RuntimeError(
        f"等待场地表数据超时；当前首列内容为：{last_labels or ['空']}"
    )


async def wait_for_date_card(
    page: Page, deadline_seconds: float = 180, refresh_interval: float = 2.0
):
    """Poll the target date card, reloading the reservation view every round.

    A reload keeps the reservation route, so the date list is requested again
    from the backend and the newly released day shows up on its own.
    """
    date_card = page.locator(
        f'.date_box > div[style*="order: {DAYS[ORDER_DAY]}"], '
        f'.date_box > div[style*="order:{DAYS[ORDER_DAY]}"]'
    ).first
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_seconds
    while loop.time() < end:
        try:
            await date_card.wait_for(state="visible", timeout=refresh_interval * 1000)
            return date_card
        except PlaywrightError:
            pass
        await page.reload(wait_until="domcontentloaded")

    raise PlaywrightTimeoutError(
        f"刷新等待超时：{deadline_seconds:g}s 内未出现 "
        f"order={DAYS[ORDER_DAY]} 的日期卡片。"
    )


async def wait_for_date_card(
    page: Page, deadline_seconds: float = 180, refresh_interval: float = 2.0
):
    """Poll the target date card, reloading the current view every interval.

    A reload keeps the reservation route (it does not go back to the venue
    list), so the date list is fetched again and the released date shows up.
    """
    # Select the card specifically assigned `order: 2` by the reservation UI.
    # Accept either browser serialization (`order: 2`) or minified (`order:2`).
    date_card = page.locator(
        f'.date_box > div[style*="order: {DAYS[ORDER_DAY]}"], '
        f'.date_box > div[style*="order:{DAYS[ORDER_DAY]}"]'
    ).first
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_seconds

    while loop.time() < end:
        try:
            await date_card.wait_for(state="visible", timeout=refresh_interval * 1000)
            return date_card
        except PlaywrightError:
            pass
        await page.reload(wait_until="domcontentloaded")

    raise PlaywrightTimeoutError(
        f"刷新等待超时：{deadline_seconds:g}s 内未出现 order={DAYS[ORDER_DAY]} 的日期卡片。"
    )


async def choose_venue_and_slot(page: Page) -> None:
    # First select the venue card by its stable HTML title, rather than its
    # visual position under the “热门场地” section.
    venue = page.locator(f'dt[title="{VENUE_TITLE}"]:visible').first
    try:
        await venue.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"登录后没有出现场馆列表：当前页面 {page.url}"
            f"（标题：{await page.title()}）。"
        ) from exc
    await venue.click()

    # Reload every 2s until the target date card is released, then stop.
    last_date = await wait_for_date_card(page)
    await last_date.click()
    print("126")
    # 下页
    next_time_page = page.locator(
        "table thead i.ivu-icon.ivu-icon-ios-arrow-forward"
    ).first
    await next_time_page.wait_for(state="visible", timeout=15_000)
    await next_time_page.click()
    # await next_time_page.click()
    # await asyncio.sleep(2)
    # Try courts in the priority order declared in VENUE in the configured
    # target time column.
    # The component contains a separate fixed-left table with duplicate court
    # labels.  Only #scrollTable holds the clickable time-slot cells.
    table_rows = await wait_for_court_table(page)
    time_headers = page.locator(
        ".reserveTable #scrollTable.spaceTable > table > thead "
        "> tr > td:not(:first-child) span"
    )
    visible_times = [
        " ".join(text.split()) for text in await time_headers.all_inner_texts()
    ]

    selected_court: int | None = None
    row_count = await table_rows.count()
    print(f"场地表共找到 {row_count} 行。")
    for court_number in VENUE:
        court_row = None
        for row_index in range(row_count):
            row = table_rows.nth(row_index)
            first_cell = row.locator(":scope > td").first
            label = "".join((await first_cell.inner_text()).split())
            if label == f"{court_number}号":
                court_row = row
                break

        if court_row is None:
            print(f"未找到 {court_number} 号场地，跳过。")
            continue

        # Time blocks already appear in chronological order under each row.
        # ORDER_TIME=0 therefore means the row's first reserveBlock directly.
        slots = court_row.locator(":scope > td > div.reserveBlock.position")
        slot_count = await slots.count()
        print(f"{court_number} 号匹配成功，共找到 {slot_count} 个时间块。")
        if ORDER_TIME < 0 or ORDER_TIME >= slot_count:
            print(
                f"{court_number} 号不存在下标 {ORDER_TIME} 的时间段，跳过。"
            )
            continue
        print(slots)
        slot = slots.nth(ORDER_TIME)
        slot_time = (
            visible_times[ORDER_TIME]
            if ORDER_TIME < len(visible_times)
            else "未知时间"
        )
        slot_text = " ".join((await slot.inner_text()).split())
        slot_classes = await slot.get_attribute("class") or ""
        slot_html = await slot.evaluate("element => element.outerHTML")
        print(
            f"[slot] 场地={court_number}号, 下标={ORDER_TIME}, "
            f"时间={slot_time!r}, "
            f"文本={slot_text!r}, class={slot_classes!r}"
        )
        print(f"[slot.outerHTML] {slot_html}")

        classes = slot_classes
        if "reserved" in classes:
            print(f"{court_number} 号已被订，尝试下一场地。")
            continue
        if "free" not in classes:
            print(f"{court_number} 号状态为 {classes!r}，跳过。")
            continue

        await slot.wait_for(state="visible", timeout=5_000)
        await slot.click()
        selected_court = court_number
        print(f"已选择 {selected_court} 号场地。")
        break

    if selected_court is None:
        await asyncio.sleep(200)
        raise SystemExit("VENUE 列表中的场地均不可预约，已退出且未提交预约。")

    # The agreement control is an iView checkbox. `check` handles its native
    # input state even when the visual checkbox is styled by its parent label.
    agreement = page.locator('input.ivu-checkbox-input').first
    await agreement.wait_for(state="attached", timeout=15_000)
    await agreement.check(force=True)

    buddy = page.locator('div.buddy-row:text-is("王传海")').first
    await buddy.wait_for(state="visible", timeout=15_000)
    await buddy.click()


async def main() -> None:
    load_dotenv()
    username = os.environ.get("BUAA_USERNAME", "")
    password = os.environ.get("BUAA_PASSWORD", "")
    open_at_raw = os.environ.get("OPEN_AT", "")
    auto_submit = env_bool(os.environ.get("AUTO_SUBMIT", "false"))
    state_path = os.environ.get("SESSION_STATE", "state.json").strip()
    if not username or not password or not open_at_raw:
        raise SystemExit("请在 .env 中设置 BUAA_USERNAME、BUAA_PASSWORD 和 OPEN_AT。")

    open_at = datetime.fromisoformat(open_at_raw).astimezone(TZ)
    login_at = open_at - timedelta(minutes=2)
    delay = (login_at - datetime.now(TZ)).total_seconds()
    # if delay > 0:
    #     print(f"将在 {login_at:%F %T %Z} 打开登录页。")
    #     await asyncio.sleep(delay)

    stored_state = state_path if state_path and os.path.exists(state_path) else None
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(storage_state=stored_state)
        page = await context.new_page()
        await login(page, username, password, state_path)
        await choose_venue_and_slot(page)
        if not auto_submit:
            print("已完成选场，程序停在提交前；请核对页面后手动提交。")
            # Keep the real browser open for manual review.  Ctrl+C ends this
            # helper after the user has submitted or decided not to reserve.
            await asyncio.Event().wait()
            return
        # This creates a real reservation. Keep it opt-in in .env.
        submit_button = page.locator('div.btn:text-is("提交")').first
        await submit_button.wait_for(state="visible", timeout=15_000)
        await submit_button.click()
        print("已点击提交；请在页面中核对预约结果。")
        await page.wait_for_timeout(65_000)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except PlaywrightTimeoutError as exc:
        raise SystemExit("页面元素未在规定时间出现，可能页面改版、未开放或需要人工验证。") from exc
