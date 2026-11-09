"""Click-order captcha recognition; run: python -m unittest discover -s tests -v."""
import base64
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

import reserve

FONT_PATH = Path(r"C:\Windows\Fonts\msyh.ttc")
CHARS = "饭兴百"
POSITIONS = [(30, 25), (140, 60), (240, 95)]
IMAGE_SIZE = (330, 155)

HTML = """<html><body>
<div class="verifybox">
  <div class="verifybox-top">请完成安全验证</div>
  <div class="verifybox-bottom">
    <div class="verify-img-out">
      <div class="verify-img-panel" style="width:330px;height:155px">
        <img src="{src}" style="width:100%;height:100%;display:block">
      </div>
    </div>
    <div class="verify-bar-area">
      <span class="verify-msg">请依次点击【饭,兴,百】</span>
    </div>
  </div>
</div>
<script>
window._clicks = [];
document.addEventListener('click', event => {{
  window._clicks.push([Math.round(event.clientX), Math.round(event.clientY)]);
}});
</script>
</body></html>"""

# Same captcha, but the prompt is not inside .verify-msg: the code must read
# the whole .verifybox text instead.
HTML_WITHOUT_VERIFY_MSG = HTML.replace('class="verify-msg"', 'class="prompt-text"')

# Same captcha, but the prompt is filled in 600 ms after the box opens.
HTML_DELAYED_PROMPT = HTML.replace("请依次点击【饭,兴,百】", "").replace(
    "</body>",
    "<script>window.setTimeout(function () {"
    " document.querySelector('.verify-msg').textContent = '请依次点击【饭,兴,百】';"
    " }, 600);</script></body>",
)

# The prompt lives outside .verifybox (e.g. in a page-level notice).
HTML_PROMPT_OUTSIDE_BOX = HTML.replace(
    '<span class="verify-msg">请依次点击【饭,兴,百】</span>', ""
).replace("</body>", '<div class="notice">请依次点击【饭,兴,百】</div></body>')

# The prompt never appears: the code must give up instead of clicking blindly.
HTML_NO_PROMPT = HTML.replace("请依次点击【饭,兴,百】", "")


def _captcha_src() -> str:
    image = Image.new("RGB", IMAGE_SIZE, (60, 90, 120))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(FONT_PATH), 36)
    for char, position in zip(CHARS, POSITIONS):
        draw.text(position, char, font=font, fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


@unittest.skipUnless(FONT_PATH.exists(), "需要微软雅黑字体绘制测试验证码")
class ClickCaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()
        self.page = await self.browser.new_page()
        await self.page.route(
            "http://captcha.test/**",
            lambda route: route.fulfill(
                body=HTML.format(src=_captcha_src()),
                content_type="text/html; charset=utf-8",
            ),
        )
        await self.page.goto("http://captcha.test/")
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_debug_dir = reserve.CAPTCHA_DEBUG_DIR
        reserve.CAPTCHA_DEBUG_DIR = Path(self.tempdir.name)

    async def asyncTearDown(self):
        reserve.CAPTCHA_DEBUG_DIR = self.original_debug_dir
        self.tempdir.cleanup()
        await self.browser.close()
        await self.playwright.stop()

    async def test_prints_image_and_clicks_in_prompt_order(self):
        solved = await reserve.try_solve_click_captcha(self.page)

        self.assertTrue(solved)
        clicks = await self.page.evaluate("window._clicks")
        # Characters were drawn at known offsets; clicks must land on their
        # centres, in the order given by 请依次点击【饭,兴,百】.
        image = self.page.locator(".verify-img-panel img").first
        rect = await image.bounding_box()
        self.assertIsNotNone(rect)
        centres = [
            [rect["x"] + x + 16.5, rect["y"] + y + 25]
            for x, y in POSITIONS
        ]
        self.assertEqual(len(clicks), 3)
        for (click_x, click_y), (centre_x, centre_y) in zip(clicks, centres):
            self.assertLessEqual(abs(click_x - centre_x), 12)
            self.assertLessEqual(abs(click_y - centre_y), 12)
        self.assertLess(clicks[0][0], clicks[1][0])

        saved = [
            path
            for path in Path(self.tempdir.name).glob("click-*.png")
            if "-box" not in path.name
        ]
        self.assertEqual(len(saved), 1)

    async def test_reads_prompt_from_whole_box_when_msg_class_missing(self):
        await self.page.set_content(
            HTML_WITHOUT_VERIFY_MSG.replace("{src}", _captcha_src())
        )

        solved = await reserve.try_solve_click_captcha(self.page)

        self.assertTrue(solved)
        self.assertEqual(len(await self.page.evaluate("window._clicks")), 3)

    async def test_waits_for_prompt_rendered_after_the_box_opens(self):
        await self.page.set_content(
            HTML_DELAYED_PROMPT.replace("{src}", _captcha_src())
        )

        solved = await reserve.try_solve_click_captcha(self.page)

        self.assertTrue(solved)
        self.assertEqual(len(await self.page.evaluate("window._clicks")), 3)

    async def test_finds_prompt_outside_the_verifybox(self):
        await self.page.set_content(
            HTML_PROMPT_OUTSIDE_BOX.replace("{src}", _captcha_src())
        )

        solved = await reserve.try_solve_click_captcha(self.page)

        self.assertTrue(solved)
        self.assertEqual(len(await self.page.evaluate("window._clicks")), 3)

    async def test_gives_up_without_clicking_when_prompt_never_appears(self):
        await self.page.set_content(HTML_NO_PROMPT.replace("{src}", _captcha_src()))

        solved = await reserve.try_solve_click_captcha(self.page, prompt_timeout=0.8)

        self.assertFalse(solved)
        self.assertEqual(await self.page.evaluate("window._clicks"), [])
