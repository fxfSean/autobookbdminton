"""One-shot BUAA venue-reservation helper.

It deliberately uses normal browser interactions, not undocumented APIs or
request flooding.  Credentials are read only from environment variables.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import re
import struct
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
VENUE = [23,24,17,18,19,20,21,22]
VENUE_MAIN = [11,12,1,2,3,4,5,6,7,8,9,10]

DAYS = [0,1,2]
ORDER_DAY = 2
# Time-slot indexes to book, in chronological order: 0 is the first time block
# after the court label. [0, 1] covers the target hour plus the hour before it,
# which is the same window as the old ORDER_TIME=1 + EXTRA_SLOTS_BEFORE=1.
# The last index is the target hour used by the single-slot fallback.
ORDER_SLOTS = [0,1]
# When no court has the whole window free, book the hours on different courts
# (same hours, interleaved) instead of giving up.
ALLOW_SPLIT_COURTS = True
# Last resort only: False lets the script book just the target hour when no
# combination of courts covers the whole window.
REQUIRE_CONSECUTIVE = True


def env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


async def timed_step(label: str, operation):
    start = asyncio.get_running_loop().time()
    print(f"[耗时] 开始：{label}", flush=True)
    try:
        result = await operation
    except BaseException:
        elapsed = asyncio.get_running_loop().time() - start
        print(f"[耗时] {label} 中断，已等待 {elapsed:.3f}s", flush=True)
        raise
    elapsed = asyncio.get_running_loop().time() - start
    print(f"[耗时] {label} 完成：{elapsed:.3f}s", flush=True)
    return result


def is_free_slot(state: dict) -> bool:
    """A block is bookable only when it says free and is not reserved."""
    return "free" in state["classes"] and "reserved" not in state["classes"]


def slot_at(court_row, index: int):
    """The index-th time block inside one court row, or ``None`` if absent."""
    return court_row.locator(
        ":scope > td > div.reserveBlock.position"
    ).nth(index)


def plan_slots(
    states_by_court: dict[int, list[dict]],
    slot_indexes: list[int],
    *,
    target_index: int,
    allow_split: bool,
    require_consecutive: bool,
    label,
) -> list[tuple[int, int]]:
    """Choose (court, time index) pairs for the requested window.

    1. Prefer one court that has every hour free.
    2. Otherwise keep the same hours but spread them over different courts.
    3. Only when ``require_consecutive`` is off, fall back to the single target
       hour.
    """
    for court_number, states in states_by_court.items():
        if all(
            index < len(states) and is_free_slot(states[index])
            for index in slot_indexes
        ):
            slots = "、".join(label(index) for index in slot_indexes)
            print(f"{court_number} 号可连订 {slots}。")
            return [(court_number, index) for index in slot_indexes]

    if allow_split and len(slot_indexes) > 1:
        used_courts: set[int] = set()
        split_slots: list[tuple[int, int]] = []
        for index in slot_indexes:
            picked = next(
                (
                    court_number
                    for court_number, states in states_by_court.items()
                    if court_number not in used_courts
                    and index < len(states)
                    and is_free_slot(states[index])
                ),
                None,
            )
            if picked is None:
                split_slots = []
                break
            used_courts.add(picked)
            split_slots.append((picked, index))
        if split_slots:
            detail = "，".join(
                f"{label(index)} → {court_number} 号"
                for court_number, index in split_slots
            )
            print(f"没有场地能连订整个时段，改为分开预订：{detail}")
            return split_slots

    if not require_consecutive:
        for court_number, states in states_by_court.items():
            if target_index < len(states) and is_free_slot(states[target_index]):
                print(
                    f"分场也凑不齐，先订 {court_number} 号 "
                    f"{label(target_index)}。"
                )
                return [(court_number, target_index)]

    return []


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


# --- 点选验证码（“请依次点击【饭,兴,百】”）识别 -----------------------------
#
# ddddocr 只会整行 OCR，没有“定位字符 + 按顺序点击”的能力。这里的做法是：
#   1. 从 .verifybox 里取出内联 base64 图片，先把图片数据完整打印并落盘；
#   2. 用 det 模型找出候选字符框，逐个裁剪后用 OCR 模型识别；
#   3. 按提示文字的顺序匹配候选框，匹配齐全才用鼠标依次点击。
# 背景复杂的点选验证码识别率有限；只要有一个字没匹配上就不点击，交回人工。

CAPTCHA_DEBUG_DIR = Path(__file__).resolve().parent / "captcha_debug"
CLICK_CAPTCHA_BOX = ".verifybox"
CLICK_CAPTCHA_PANEL = ".verify-img-panel"
CLICK_CAPTCHA_MSG = ".verify-msg"

_ocr_engine = None
_det_engine = None


def _get_captcha_engines():
    global _ocr_engine, _det_engine
    if _ocr_engine is None or _det_engine is None:
        try:
            import ddddocr
        except ImportError as exc:
            raise RuntimeError(
                "未安装 ddddocr，请先执行："
                r".venv\Scripts\python.exe -m pip install ddddocr"
            ) from exc
        _ocr_engine = ddddocr.DdddOcr(show_ad=False)
        _det_engine = ddddocr.DdddOcr(det=True, show_ad=False)
    return _ocr_engine, _det_engine


def _decode_image_src(src: str) -> bytes:
    """Decode a data: URL; the captcha image is embedded as base64."""
    header, sep, payload = src.partition(",")
    if not sep or not header.startswith("data:"):
        raise ValueError("验证码 img 的 src 不是 data: URL")
    if ";base64" not in header:
        raise ValueError("验证码 img 不是 base64 编码")
    return base64.b64decode(payload)


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None


def _detect_captcha_chars(png: bytes, stem: Path) -> list[dict]:
    """Detect candidate character boxes, then OCR each crop (blocking)."""
    from PIL import Image

    ocr, det = _get_captcha_engines()
    image = Image.open(io.BytesIO(png)).convert("RGB")
    boxes = det.detection(png)
    stem.parent.mkdir(parents=True, exist_ok=True)
    print(f"[验证码] 目标检测得到 {len(boxes)} 个候选框：{boxes}", flush=True)

    candidates: list[dict] = []
    for index, box in enumerate(boxes):
        x1, y1, x2, y2 = (int(value) for value in box)
        pad = 4
        crop = image.crop(
            (
                max(0, x1 - pad),
                max(0, y1 - pad),
                min(image.width, x2 + pad),
                min(image.height, y2 + pad),
            )
        )
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        crop_bytes = buffer.getvalue()
        crop_path = stem.with_name(f"{stem.name}-box{index}.png")
        crop_path.write_bytes(crop_bytes)
        text = ocr.classification(crop_bytes).strip()
        candidates.append({"box": [x1, y1, x2, y2], "text": text, "crop": crop_path})
        print(
            f"[验证码] 候选 {index}: box=({x1},{y1},{x2},{y2}) "
            f"识别={text!r} 裁剪={crop_path.name}",
            flush=True,
        )
    return candidates


_PROMPT_RE = re.compile(
    r"(?:请依次点击[^【\[（(]*)?[【\[（(](?P<chars>[^】\]）)]+)[】\]）)]"
)
# The body may contain unrelated 【…】 text, so it must carry the prompt prefix.
_BODY_PROMPT_RE = re.compile(
    r"请依次点击[^【\[（(]{0,10}[【\[（(](?P<chars>[^】\]）)]+)[】\]）)]"
)


async def _wait_for_click_captcha_box(page: Page, timeout_seconds: float = 15.0):
    """Return the first visible .verifybox, waiting for the widget to open."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        for frame in page.frames:
            locator = frame.locator(f"{CLICK_CAPTCHA_BOX}:visible")
            try:
                if await locator.count():
                    return locator.first
            except PlaywrightError:
                continue
        await asyncio.sleep(0.2)
    return None


async def _read_click_captcha_prompt(
    page: Page, box_locator, timeout_seconds: float = 10.0
) -> tuple[str, str]:
    """Poll until 请依次点击【…】 is rendered, then parse the click order.

    The widget fills the prompt after its image request returns, so the text can
    still be empty at the moment .verifybox becomes visible.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last_text = ""
    while loop.time() < deadline:
        texts: list[tuple[str, bool]] = []  # (text, requires 请依次点击 prefix)
        for source in (box_locator.locator(CLICK_CAPTCHA_MSG), box_locator):
            try:
                if await source.count():
                    texts.append((await source.first.text_content() or "", False))
            except PlaywrightError:
                pass
        try:
            # Last resort: some skins put the prompt outside the box.
            texts.append((await page.inner_text("body"), True))
        except PlaywrightError:
            pass
        for text, require_prefix in texts:
            match = (_BODY_PROMPT_RE if require_prefix else _PROMPT_RE).search(
                text or ""
            )
            if match:
                targets = re.sub(r"[,，、\s]", "", match.group("chars"))
                if 0 < len(targets) <= 8:
                    return match.group(0).strip(), targets
        last_text = next(
            (text.strip() for text, _ in texts if text and text.strip()), last_text
        )
        await asyncio.sleep(0.2)
    return last_text[:200], ""


async def _dump_captcha_diagnostics(page: Page, box_locator) -> None:
    """Print what the page actually contains when the prompt cannot be read."""
    try:
        boxes = page.locator(CLICK_CAPTCHA_BOX)
        count = await boxes.count()
        print(f"[验证码] 页面上 .verifybox 元素数量：{count}", flush=True)
        for index in range(min(count, 3)):
            info = await boxes.nth(index).evaluate(
                """el => ({
                    visible: !!(el.offsetWidth || el.offsetHeight),
                    text: (el.textContent || '').trim().slice(0, 200),
                    html: el.outerHTML.slice(0, 1200),
                })"""
            )
            print(f"[验证码] box{index}: {info}", flush=True)
        snippet = " ".join((await page.inner_text("body")).split())
        print(f"[验证码] body 文本片段：{snippet[:300]!r}", flush=True)
        for frame in page.frames:
            slider = frame.locator(".verify-move-block, .verify-slider")
            if await slider.count():
                print(
                    f"[验证码] 检测到滑块元素 {await slider.count()} 个"
                    f"（{frame.url[:80]}），可能不是点选验证码。",
                    flush=True,
                )
    except PlaywrightError as exc:
        print(f"[验证码] 诊断信息读取失败：{exc}", flush=True)


async def try_solve_click_captcha(page: Page, prompt_timeout: float = 10.0) -> bool:
    """Print the captcha image data, try to read it, click it in order."""
    box_locator = await _wait_for_click_captcha_box(page)
    if box_locator is None:
        print("[验证码] 未出现可见的 .verifybox，跳过识别。", flush=True)
        return False

    message, targets = await _read_click_captcha_prompt(
        page, box_locator, prompt_timeout
    )
    print(f"[验证码] 提示文字：{message!r}", flush=True)
    print(f"[验证码] 解析出的点击顺序：{targets!r}", flush=True)

    image = box_locator.locator(CLICK_CAPTCHA_PANEL).first.locator("img").first
    if not await image.count():
        image = box_locator.locator("img").first
    src = await image.get_attribute("src") or ""
    if not src.startswith("data:"):
        print(f"[验证码] 未找到内联图片，src={src[:60]!r}", flush=True)
        return False

    # ---- 识别前，先把图片数据完整打印并落盘 ----
    png = _decode_image_src(src)
    digest = hashlib.sha1(png).hexdigest()
    size = _png_size(png)
    CAPTCHA_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    image_path = CAPTCHA_DEBUG_DIR / f"click-{stamp}-{digest[:8]}.png"
    image_path.write_bytes(png)
    rect = await image.bounding_box()
    element_info = await image.evaluate(
        """el => ({
            tag: el.tagName,
            cls: el.className,
            natural: [el.naturalWidth, el.naturalHeight],
            rect: [el.getBoundingClientRect().width,
                   el.getBoundingClientRect().height],
            dpr: window.devicePixelRatio,
        })"""
    )
    print(
        "[验证码] 图片数据：\n"
        f"  src 前缀    : {src[:48]}…\n"
        f"  src 长度    : {len(src)} 字符\n"
        f"  base64 长度 : {len(src.partition(',')[2])} 字符\n"
        f"  解码字节数  : {len(png)}\n"
        f"  sha1        : {digest}\n"
        f"  PNG 尺寸    : {size}\n"
        f"  元素信息    : {element_info}\n"
        f"  已保存到    : {image_path}\n"
        f"  前 32 字节  : {png[:32].hex(' ')}",
        flush=True,
    )

    if not targets:
        print("[验证码] 没有解析到顺序文字，跳过自动识别。", flush=True)
        await _dump_captcha_diagnostics(page, box_locator)
        return False

    try:
        candidates = await asyncio.to_thread(
            _detect_captcha_chars, png, image_path.with_suffix("")
        )
    except Exception as exc:  # noqa: BLE001 - 识别失败不应打断预订流程
        print(f"[验证码] 识别失败：{exc}", flush=True)
        return False

    ordered: list[dict] = []
    used: set[int] = set()
    for target in targets:
        hit = next(
            (
                index
                for index, item in enumerate(candidates)
                if index not in used and item["text"] == target
            ),
            None,
        )
        if hit is None:
            hit = next(
                (
                    index
                    for index, item in enumerate(candidates)
                    if index not in used and target in item["text"]
                ),
                None,
            )
        if hit is None:
            break
        used.add(hit)
        ordered.append(candidates[hit])

    matched = "，".join(
        f"{target}->{item['text']!r}" for target, item in zip(targets, ordered)
    )
    print(f"[验证码] 匹配结果：{matched or '无'}", flush=True)

    if len(ordered) != len(targets):
        print(
            f"[验证码] 只识别出 {len(ordered)}/{len(targets)} 个目标字符，"
            "为避免误点，不自动点击，请手动完成。",
            flush=True,
        )
        return False

    if not env_bool(os.environ.get("CAPTCHA_AUTO_CLICK", "true")):
        print("[验证码] CAPTCHA_AUTO_CLICK=false，仅识别不点击。", flush=True)
        return False

    if rect is None or size is None:
        print("[验证码] 缺少点击坐标信息，请手动完成。", flush=True)
        return False

    width, height = size
    for target, item in zip(targets, ordered):
        x1, y1, x2, y2 = item["box"]
        css_x = rect["x"] + ((x1 + x2) / 2) * rect["width"] / width
        css_y = rect["y"] + ((y1 + y2) / 2) * rect["height"] / height
        await page.mouse.click(css_x, css_y)
        print(f"[验证码] 已点击 {target!r} @ ({css_x:.1f}, {css_y:.1f})", flush=True)
        await asyncio.sleep(0.2)
    return True


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


async def choose_venue_and_slot(page: Page, *, resume_after_reload: bool = False) -> None:
    # First select the venue card by its stable HTML title, rather than its
    # visual position under the “热门场地” section.
    venue = page.locator(f'dt[title="{VENUE_TITLE}"]:visible').first
    if resume_after_reload:
        print("[重选] 等待场馆列表或日期面板出现（最多15秒）…", flush=True)
        # A refreshed reservation route can already be inside the venue.
        await timed_step("等待场馆/日期面板", venue.or_(page.locator(".date_box")).first.wait_for(
            state="visible", timeout=15_000
        ))
    if not resume_after_reload or await venue.is_visible():
        try:
            await venue.wait_for(state="visible", timeout=15_000)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                f"登录后没有出现场馆列表：当前页面 {page.url}"
                f"（标题：{await page.title()}）。"
            ) from exc
        await timed_step("点击场馆", venue.click())

    # Reload every 2s until the target date card is released, then stop.
    if resume_after_reload:
        print("[重选] 选择配置的日期…", flush=True)
    last_date = await timed_step("等待目标日期", wait_for_date_card(page))
    await timed_step("点击目标日期", last_date.click())
    # 下页
    if resume_after_reload:
        print("[重选] 切换时间段页面…", flush=True)
    next_time_page = page.locator(
        "table thead i.ivu-icon.ivu-icon-ios-arrow-forward"
    ).first
    await timed_step("等待时段翻页按钮", next_time_page.wait_for(state="visible", timeout=15_000))
    await timed_step("点击时段翻页按钮", next_time_page.click())
    # await next_time_page.click()
    # await asyncio.sleep(2)
    # Try courts in the priority order declared in VENUE in the configured
    # target time column.
    # The component contains a separate fixed-left table with duplicate court
    # labels.  Only #scrollTable holds the clickable time-slot cells.
    table_rows = await timed_step("等待场地表数据", wait_for_court_table(page))
    time_headers = page.locator(
        ".reserveTable #scrollTable.spaceTable > table > thead "
        "> tr > td:not(:first-child) span"
    )
    visible_times = [
        " ".join(text.split()) for text in await time_headers.all_inner_texts()
    ]

    # Book every configured slot, so one court covers the whole window
    # (e.g. 12:00-14:00 for ORDER_SLOTS = [0, 1]).
    slot_indexes = sorted({index for index in ORDER_SLOTS if index >= 0})
    if not slot_indexes:
        raise RuntimeError("ORDER_SLOTS 为空：请至少配置一个时间段下标。")
    debug_slots = env_bool(os.environ.get("DEBUG_SLOTS", "false"))

    def slot_label(index: int) -> str:
        if index < len(visible_times):
            return visible_times[index]
        return f"下标{index}"

    # Read row labels once, rather than one browser round trip per row for
    # every preferred court (up to len(VENUE) * row_count reads).
    row_labels = await timed_step("读取全部场地行标签", table_rows.evaluate_all(
        "rows => rows.map(row => row.querySelector('td')?.textContent || '')"
    ))
    row_by_label = {}
    for index, label in enumerate(row_labels):
        row_by_label.setdefault("".join(label.split()), index)
    row_count = len(row_labels)
    print(f"场地表共找到 {row_count} 行。")

    # (row locator, per-block state) for every preferred court, read once.
    courts: dict[int, tuple[object, list[dict]]] = {}
    for court_number in VENUE:
        row_index = row_by_label.get(f"{court_number}号")
        if row_index is None:
            print(f"未找到 {court_number} 号场地，跳过。")
            continue
        court_row = table_rows.nth(row_index)

        # Time blocks already appear in chronological order under each row.
        # Index 0 therefore means the row's first reserveBlock directly.
        slots = court_row.locator(":scope > td > div.reserveBlock.position")
        slot_count = await timed_step(f"{court_number}号：读取时间块数量", slots.count())
        states = await timed_step(
            f"{court_number}号：读取时间块状态",
            slots.evaluate_all(
                """(blocks, withHtml) => blocks.map(block => ({
                    text: block.textContent || '',
                    classes: block.className || '',
                    html: withHtml ? block.outerHTML : ''
                }))""",
                debug_slots,
            ),
        )
        courts[court_number] = (court_row, states)
        print(f"{court_number} 号匹配成功，共找到 {slot_count} 个时间块。")
        for index in slot_indexes:
            if index >= len(states):
                print(f"{court_number} 号不存在下标 {index} 的时间块。")
                continue
            state = states[index]
            print(
                f"[slot] 场地={court_number}号, 下标={index}, "
                f"时间={slot_label(index)!r}, "
                f"文本={' '.join(state['text'].split())!r}, "
                f"class={state['classes']!r}"
            )
            if debug_slots:
                print(f"[slot.outerHTML] {state['html']}")

    chosen_slots = plan_slots(
        {court_number: states for court_number, (_, states) in courts.items()},
        slot_indexes,
        target_index=slot_indexes[-1],
        allow_split=ALLOW_SPLIT_COURTS,
        require_consecutive=REQUIRE_CONSECUTIVE,
        label=slot_label,
    )

    for court_number, index in chosen_slots:
        court_row, _ = courts[court_number]
        slot = slot_at(court_row, index)
        await timed_step(
            f"{court_number}号：等待下标 {index} 可见",
            slot.wait_for(state="visible", timeout=5_000),
        )
        await timed_step(
            f"{court_number}号：点击下标 {index}", slot.click()
        )
        print(f"已选择 {court_number} 号 {slot_label(index)}。")

    # A click that did nothing usually means the table re-rendered between our
    # snapshot and the click, or an earlier selection was replaced.
    checked_courts: set[int] = set()
    for court_number, _ in chosen_slots:
        if court_number in checked_courts:
            continue
        checked_courts.add(court_number)
        court_row, _ = courts[court_number]
        after_classes = await court_row.locator(
            ":scope > td > div.reserveBlock.position"
        ).evaluate_all(
            "blocks => blocks.map(block => block.className || '')"
        )
        still_free = [
            index
            for court, index in chosen_slots
            if court == court_number
            and index < len(after_classes)
            and "free" in after_classes[index]
        ]
        if still_free:
            print(
                f"警告：{court_number} 号下标 {still_free} 点击后仍显示可预约，"
                f"请检查是否真的选中。"
            )

    if not chosen_slots:
        if resume_after_reload:
            raise RuntimeError("VENUE 列表中的场地均不可预约，本轮未提交。")
        await asyncio.sleep(200)
        raise SystemExit("VENUE 列表中的场地均不可预约，已退出且未提交预约。")

    # The agreement control is an iView checkbox. `check` handles its native
    # input state even when the visual checkbox is styled by its parent label.
    agreement = page.locator('input.ivu-checkbox-input').first
    await timed_step("等待协议复选框", agreement.wait_for(state="attached", timeout=15_000))
    await timed_step("勾选协议", agreement.check(force=True))

    buddy = page.locator('div.buddy-row:text-is("王传海")').first
    await timed_step("等待同伴", buddy.wait_for(state="visible", timeout=15_000))
    await timed_step("选择同伴", buddy.click())


async def reserve_with_manual_reload(page: Page, auto_submit: bool) -> None:
    """Only a reload after a submit click starts another reservation round."""
    armed = False
    submitted_document = None
    pending_reload = asyncio.Event()
    closed = asyncio.Event()

    def mark_submitted(source) -> None:
        nonlocal armed
        if source["page"] == page:
            armed = True
            print("已记录提交，等待手动刷新（Command+R）重试。")

    # Observe Chromium's navigation intent before redirects occur. The final
    # document's PerformanceNavigationTiming can say 'navigate' after a reload
    # redirects, so it cannot reliably identify a browser toolbar/Command+R reload.
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Page.enable")
    main_frame_id = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]

    def on_navigation(event) -> None:
        nonlocal armed
        if (
            armed
            and event.get("frameId") == main_frame_id
            and event.get("navigationType") == "reload"
        ):
            armed = False
            pending_reload.set()
            print("检测到浏览器刷新，已启动重新选场任务。", flush=True)

    cdp.on("Page.frameStartedNavigating", on_navigation)

    def on_close() -> None:
        closed.set()
        pending_reload.set()

    # Capture manual and scripted clicks alike, including clicks on children
    # of the submit control. Reinstall the hook in every new document.
    click_hook = """() => {
        if (window.__reservationSubmitHook) return;
        window.__reservationSubmitHook = true;
        window.__reservationDocumentId = Date.now().toString() + Math.random().toString();
        document.addEventListener('click', event => {
            const button = event.target instanceof Element
                ? event.target.closest('div.btn, button, input[type="submit"], [role="button"]') : null;
            const text = button && (button.textContent || button.value || '').replace(/\\s/g, '');
            if (text === '提交') {
                window.markReservationSubmitted().catch(() => {});
            }
        }, true);
    }"""
    await page.expose_binding("markReservationSubmitted", mark_submitted)
    await page.add_init_script(f"({click_hook})();")
    for frame in page.frames:
        await frame.evaluate(click_hook)
    page.on("close", on_close)

    async def submit_or_pause() -> None:
        nonlocal armed, submitted_document
        submitted_document = await page.evaluate("window.__reservationDocumentId")
        if not auto_submit:
            print("已完成选场，请核对后手动提交；提交后刷新网页可重新选场。")
            return
        submit_button = page.locator('div.btn:text-is("提交")').first
        await timed_step("等待提交按钮", submit_button.wait_for(state="visible", timeout=15_000))
        await timed_step("点击提交", submit_button.click(no_wait_after=True, timeout=15_000))
        armed = True
        print("已点击提交，正在尝试自动完成验证码；识别失败时请手动完成。")
        # 定位验证码
        try:
            solved = await try_solve_click_captcha(page)
        except PlaywrightError as exc:
            solved = False
            print(f"[验证码] 处理验证码时页面异常：{exc}", flush=True)
        if solved:
            print("[验证码] 已按提示顺序完成点选，请检查提交结果。", flush=True)


    async def run_round(retry: bool) -> None:
        if retry:
            # Wait for the new document rather than another navigation callback.
            print("[重选] 确认已进入刷新后的新文档（最多15秒）…", flush=True)
            await timed_step("刷新后等待新文档", page.wait_for_function(
                "old => !!window.__reservationDocumentId && window.__reservationDocumentId !== old",
                arg=submitted_document,
                timeout=15_000,
            ))
            print("[重选] 新文档已就绪，开始原有预订流程。", flush=True)
        await choose_venue_and_slot(page, resume_after_reload=retry)
        await submit_or_pause()

    # The refresh dispatcher must run independently of the active booking
    # operation: a click can remain awaiting navigation after its handler has
    # already opened the captcha. Previously that kept the refresh consumer
    # from ever starting, despite the callback printing that it had fired.
    submitted_document = await page.evaluate("window.__reservationDocumentId")
    round_task = asyncio.create_task(timed_step("首轮预订总计", run_round(False)))
    reload_task = asyncio.create_task(pending_reload.wait())
    close_task = asyncio.create_task(closed.wait())
    tasks = {round_task, reload_task, close_task}
    try:
        while True:
            waiting = {reload_task, close_task}
            if round_task is not None:
                waiting.add(round_task)
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if close_task in done:
                break
            if reload_task in done:
                pending_reload.clear()
                if round_task is not None:
                    if not round_task.done():
                        print("[重选] 中止上一轮等待，立即接管刷新后的页面。", flush=True)
                        round_task.cancel()
                    await asyncio.gather(round_task, return_exceptions=True)
                    tasks.discard(round_task)
                armed = False
                round_task = asyncio.create_task(timed_step("刷新重选总计", run_round(True)))
                reload_task = asyncio.create_task(pending_reload.wait())
                tasks = {round_task, reload_task, close_task}
                continue
            if round_task is not None and round_task in done:
                error = round_task.exception()
                tasks.discard(round_task)
                round_task = None
                if error is not None:
                    if isinstance(error, (PlaywrightError, RuntimeError)):
                        armed = True
                        print(f"本轮未完成：{error}；可刷新重试。", flush=True)
                    else:
                        raise error
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        page.remove_listener("close", on_close)
        cdp.remove_listener("Page.frameStartedNavigating", on_navigation)
        if not page.is_closed():
            try:
                await cdp.detach()
            except PlaywrightError:
                # The browser may close between the state check and detach.
                pass


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
        await reserve_with_manual_reload(page, auto_submit)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except PlaywrightTimeoutError as exc:
        raise SystemExit("页面元素未在规定时间出现，可能页面改版、未开放或需要人工验证。") from exc
