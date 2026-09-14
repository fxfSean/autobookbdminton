# 场地预约助手

该工具在开放前两分钟打开统一认证登录页，登录后按 HTML 的 `dt[title="学院路校区-体育馆训练馆(副馆)"]` 精确选择场馆；该点击会直接进入订场页。随后选择配置的日期，在 `VENUE` 场地优先级列表中挑选目标时间段的首个可预约场地，并勾选 iView 同意复选框。

它采用浏览器正常操作，不调用未公开接口、不绕过验证码，也不会高频刷新。若出现验证码或额外身份验证，需由本人完成。

## 安装

Windows：
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env

在 macOS 的终端中执行：
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env
```

编辑 `.env`：填写账号、密码与实际开放时间。`.env` 已被 Git 忽略，勿把凭据写进代码或发到聊天记录。

首次请保持 `AUTO_SUBMIT=false` 进行演练：程序会停在提交前，由你检查场地、日期和时段。确认页面选择无误后，才将其改为 `true`，使其点击提交。

## 运行

```bash
source .venv/bin/activate
python3 reserve.py
```

## 注意

- 预约成功与否仍受平台开放规则、资格、库存和网络状况限制。
- 网站若改版，可能需要根据页面实际的文本或 CSS 选择器微调 `reserve.py`。
- 使用前请确认符合场馆系统的预约规则。 
