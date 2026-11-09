"""Browser regressions; run: python -m unittest discover -s tests -v."""
import asyncio
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright

import reserve
import reserve_main


class ReloadTests(unittest.IsolatedAsyncioTestCase):
    module = reserve
    priority_name = "VENUE"
    numbers = (20, 21)
    venue_title = reserve.VENUE_TITLE

    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()
        self.page = await self.browser.new_page()
        await self.page.route(
            "http://reservation.test/**",
            lambda route: route.fulfill(
                body='<div class="btn" onclick="document.body.dataset.submitted=1">提交</div>',
                content_type="text/html; charset=utf-8",
            ),
        )
        await self.page.goto("http://reservation.test/")
        self.cdp = await self.page.context.new_cdp_session(self.page)
        self.task = None

    async def asyncTearDown(self):
        await self.page.close()
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 3)
            finally:
                await self.browser.close()
                await self.playwright.stop()
        else:
            await self.browser.close()
            await self.playwright.stop()

    async def test_refresh_interrupts_unfinished_previous_round(self):
        """The submit hook may fire while an earlier click is still awaiting."""
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        retried = asyncio.Event()

        async def choose(page, *, resume_after_reload=False):
            if resume_after_reload:
                retried.set()
                return
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(self.module, "choose_venue_and_slot", choose):
            self.task = asyncio.create_task(
                self.module.reserve_with_manual_reload(self.page, False)
            )
            await asyncio.wait_for(entered.wait(), 3)
            await self.page.locator(".btn").click()
            await self.cdp.send("Page.reload")
            await asyncio.wait_for(retried.wait(), 3)
            self.assertTrue(cancelled.is_set())

    async def test_only_post_submit_reload_retries_without_internal_loop(self):
        calls = []
        entered = asyncio.Event()
        retried = asyncio.Event()

        async def choose(page, *, resume_after_reload=False):
            calls.append(resume_after_reload)
            if resume_after_reload:
                await page.reload()
                retried.set()
            else:
                entered.set()

        with patch.object(self.module, "choose_venue_and_slot", choose):
            self.task = asyncio.create_task(
                self.module.reserve_with_manual_reload(self.page, False)
            )
            await asyncio.wait_for(entered.wait(), 3)
            await self.page.reload()
            await asyncio.sleep(0.1)
            self.assertEqual(calls, [False])
            await self.page.locator(".btn").click()
            await self.cdp.send("Page.reload")
            await asyncio.wait_for(retried.wait(), 3)
            await asyncio.sleep(0.1)
            self.assertEqual(calls, [False, True])

    async def test_automatic_submission_resumes_after_reload(self):
        calls = []

        async def choose(page, *, resume_after_reload=False):
            calls.append(resume_after_reload)

        with patch.object(self.module, "choose_venue_and_slot", choose):
            self.task = asyncio.create_task(
                self.module.reserve_with_manual_reload(self.page, True)
            )
            await self.page.wait_for_function("document.body.dataset.submitted === '1'")
            old = await self.page.evaluate("window.__reservationDocumentId")
            await self.cdp.send("Page.reload")
            await self.page.wait_for_function(
                "old => window.__reservationDocumentId !== old && document.body?.dataset.submitted === '1'",
                arg=old,
                timeout=3000,
            )
            self.assertEqual(calls, [False, True])

    async def test_selects_available_court_in_priority_order(self):
        def row(number, state):
            return f'''<tr><td> {number} 号 </td>
                <td><div class="reserveBlock position free">可约</div></td>
                <td><div class="reserveBlock position {state}"
                onclick="document.body.dataset.court='{number}'">时段</div></td></tr>'''

        await self.page.set_content(f'''
            <dt title="{self.venue_title}">场馆</dt>
            <div class="date_box"><div style="order:2">日期</div></div>
            <table><thead><tr><td><i class="ivu-icon ivu-icon-ios-arrow-forward">下一页</i></td></tr></thead></table>
            <div class="reserveTable"><div id="scrollTable" class="spaceTable"><table>
            <thead><tr><td>场地</td><td><span>18:00</span></td><td><span>19:00</span></td></tr></thead>
            <tbody class="ivu-table-tbody">{row(self.numbers[1], 'free')}{row(self.numbers[0], 'reserved')}</tbody>
            </table></div></div>
            <input type="checkbox" class="ivu-checkbox-input">
            <div class="buddy-row">王传海</div>
        ''')
        with patch.object(self.module, self.priority_name, list(self.numbers)), patch.object(self.module, "ORDER_DAY", 2), patch.object(self.module, "ORDER_SLOTS", [0, 1]):
            await self.module.choose_venue_and_slot(self.page)
        self.assertEqual(await self.page.locator('body').get_attribute('data-court'), str(self.numbers[1]))
        self.assertTrue(await self.page.locator('input').is_checked())


class MainVenueReloadTests(ReloadTests):
    module = reserve_main
    priority_name = "VENUE_MAIN"
    numbers = (1, 2)
    venue_title = reserve_main.VENUE_MAIN_TITLE
