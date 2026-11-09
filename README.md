# 场地预约助手

该工具在开放前两分钟打开统一认证登录页，登录后按 HTML 的 `dt[title="学院路校区-体育馆训练馆(副馆)"]` 精确选择场馆；该点击会直接进入订场页。随后选择配置的日期，在 `VENUE` 场地优先级列表中挑选目标时间段的首个可预约场地，并勾选 iView 同意复选框。


## 安装

需要 Python 3.9 或更高版本（已在 3.13 上验证）。

Windows（PowerShell）：
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
```

macOS（终端）：
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env
```

`.venv` 与创建它的那台机器绑定，其中记录了 Python 的绝对路径。不要把 `.venv` 从别的电脑复制过来，应该在每台机器上各自执行上面的命令重建。

编辑 `.env`：填写账号、密码与实际开放时间。`.env` 已被 Git 忽略，勿把凭据写进代码或发到聊天记录。

首次请保持 `AUTO_SUBMIT=false` 进行演练：程序会停在提交前，由你检查场地、日期和时段。确认页面选择无误后，才将其改为 `true`，使其点击提交。

## 登录状态

程序会把登录后的 cookies 保存到 `state.json`（`.env` 里的 `SESSION_STATE`，留空则每次重新登录）。下次运行先带着这份会话访问：仍然有效就直接跳过登录，也就不会遇到验证码。

会话过期时，程序会自动填好账号密码：

- 没有验证码：自动点击“登录”。
- 出现验证码：程序停下来，等你在浏览器窗口里输入验证码并点击“登录”，最多等 5 分钟。

`state.json` 等同于你的账号凭据，已被 Git 忽略，不要发给别人。换账号或会话异常时删掉它即可。

## 运行

```bash
source .venv/bin/activate
python3 reserve.py
```

## 提交后的点选验证码

提交后如果弹出 `.verifybox`（例如“请依次点击【饭,兴,百】”），`reserve.py` 会：

1. 从 `.verify-img-panel img` 读取内联的 base64 PNG，打印 `src` 长度、解码字节数、sha1、PNG 尺寸、元素坐标，并把原图和每个候选字的裁剪图存到 `captcha_debug/`；
2. 用 ddddocr 的 `det` 模型找候选字符框，再逐个裁剪、OCR 识别；
3. 按提示文字的顺序匹配，**三个字都匹配上才**用鼠标依次点击；缺任意一个字就只打印结果、不点击，交给你手动完成。

`.env` 里 `CAPTCHA_AUTO_CLICK=false` 可关闭自动点击（仍会识别并打印）。点选验证码依赖背景复杂度，识别率没有保证；`captcha_debug/` 下的原图和裁剪图可用于判断是哪一步没认出来，从而调整思路或补充训练数据。

## 注意

- 点击“提交”后，脚本会持续等待：手动刷新整个网页即可重新读取场地列表，按 `VENUE` 优先级选择目标时段的首个可用场地。`AUTO_SUBMIT=true` 时重新提交，否则停在提交前。出现“请依次点击【…】”的点选验证码时，脚本会先尝试自动识别（见上节）；识别不完整时回退为人工点击。刷新表示你主动要求重试，脚本不判断上一单是否成功；预约成功后请关闭浏览器或按 Ctrl+C 结束，不要再刷新。
- 预约成功与否仍受平台开放规则、资格、库存和网络状况限制。
- 网站若改版，可能需要根据页面实际的文本或 CSS 选择器微调 `reserve.py`。
- 使用前请确认符合场馆系统的预约规则。

## 连订多小时

`reserve.py` 顶部有几个常量控制订哪几个时段、以及凑不齐时怎么办：

```python
ORDER_SLOTS = [0, 1]     # 要预订的时间段下标，0 是该场地第一个时间块
ALLOW_SPLIT_COURTS = True
REQUIRE_CONSECUTIVE = True
```

默认 `ORDER_SLOTS = [0, 1]`，即第一个和第二个时间块（例如 12:00-13:00、13:00-14:00），共 2 小时。程序会先排序并去重；列表**最后一个下标**同时作为 `REQUIRE_CONSECUTIVE=False` 时“只订一小时”的兜底目标。程序按 `VENUE` 顺序依次尝试：

1. **同一块场地连订**：两个时段都空闲 → 直接选中，最优先
2. **分场地预订**（`ALLOW_SPLIT_COURTS=True`）：没有任何场地能连订时，保持同样的两个小时，但拆到不同场地——每个小时各自按 `VENUE` 优先级找第一块空闲场地，且不会重复用同一块
3. **只订目标时段**（仅当 `REQUIRE_CONSECUTIVE=False`）：连分场都凑不齐时，退而只订目标那一小时

日志会明确写出走了哪条路径：

```
20 号可连订 12:00-13:00、13:00-14:00。          # 同一场地
没有场地能连订整个时段，改为分开预订：12:00-13:00 → 21 号，13:00-14:00 → 20 号。
```

选中后还会复查每个块的 class，如果点击后仍显示 `free` 会打印警告（说明该块没真正选上，或被后面的选择顶掉了）。

## 常见问题

**运行 `python3` 时弹出 “xcode-select: note: No developer tools were found”。**
说明这台机器上没有真正的 Python，`/usr/bin/python3` 只是 Apple 的占位程序。请从 python.org 安装官方 Python 后再重建 `.venv`（或执行 `xcode-select --install` 安装命令行工具）。

**`playwright install chromium` 长时间没有进度。**
官方 CDN 在国内可能很慢。可先从 `https://cdn.npmmirror.com/binaries/chrome-for-testing/<版本>/mac-arm64/chrome-mac-arm64.zip` 下载，解压到 `~/Library/Caches/ms-playwright/chromium-<revision>`，并在该目录下新建空文件 `INSTALLATION_COMPLETE`。具体版本与 revision 用 `python3 -m playwright install --dry-run chromium` 查看。
