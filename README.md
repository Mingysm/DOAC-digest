# DOAC 自动摘要推送

定时抓取《The Diary Of A CEO》更新 → 取文字稿 → 生成中文笔记 → 邮件发到 iPhone。

**全部运行在 GitHub Actions 上，iPhone 端零安装**（用系统自带的"邮件"App 收就行）。
成本：0 元。

---

## 它怎么做到的

| 环节 | 方案 | 为什么 |
|---|---|---|
| 定时 | GitHub Actions cron | 公开仓库免费、不限时长 |
| 抓更新 | 官方 RSS `rss2.flightcast.com/xmsftuzjjykcmqwolaqn6mdn` | 无需 API key，含标题/音频/时长/描述 |
| 去重 | `state/seen.json` 记已处理 guid，自动 commit 回仓库 | 不重复处理、不重复发信 |
| **拿文字稿** | **直接用 RSS 自带的官方 transcript**（`<podcast:transcript>`，vtt 文件） | 关键省钱点：DOAC 官方 RSS 里就带了逐句字幕，下载即用，不用 YouTube、不用付费转录。886 集中最近 153 集都有，**新集基本 100% 覆盖** |
| 兜底 1 | 旧集没有 transcript → 搜 YouTube 抓自动字幕 | 提高覆盖率 |
| 兜底 2 | 还是拿不到 → 下载 mp3 → ffmpeg 压到 16kbps → Groq Whisper 转录 | 最后防线 |
| 摘要 | map-reduce：分段提取 → 汇总成结构化中文笔记 | 单集 2~3 万字，一次塞不进 prompt |
| 推送 | SMTP 邮件（Gmail / QQ / Outlook 等任意 SMTP） | 免费，长文也好看，天然存档 |

---

## 三步配置

### 1. 建仓库并上传

```bash
cd doac-digest
git init && git add . && git commit -m "init"
gh repo create doac-digest --public --source=. --push   # 或手动在 GitHub 建库后 push
```

> 用 **公开仓库**：Actions 时长免费不限量，且 Secrets 依然加密安全。

### 2. 填 Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**

**必填（邮件）**：

| Secret | 值 | 说明 |
|---|---|---|
| `SMTP_HOST` | `smtp.163.com` | 163 网易邮箱；Gmail 填 `smtp.gmail.com`，QQ 填 `smtp.qq.com`，Outlook 填 `smtp.office365.com` |
| `SMTP_PORT` | `465` | **163/QQ 必须填 465**（SSL）；Gmail 用 `587` |
| `SMTP_USER` | `you@163.com` | 完整邮箱地址 |
| `SMTP_PASS` | `xxxxxxxxxxxxxxxx` | **163 授权码**（不是登录密码！），获取方式见下 |
| `MAIL_TO` | `you@163.com` | 收件地址，多个用逗号分隔 |

### 163 授权码怎么拿（一次性操作，2 分钟）

1. 电脑浏览器登录 [mail.163.com](https://mail.163.com)
2. 顶部 **设置** → 左侧 **POP3/SMTP/IMAP**
3. 找到 **SMTP 服务**，点 **开启**（需要手机验证码）
4. 开启后点 **新增授权密码** → 短信验证 → 得到 **16 位授权码**（只显示一次，立刻复制保存）
5. 这串授权码就是 `SMTP_PASS` 的值

**三选一填一个（免费 LLM，脚本自动识别）**：

> ⚠️ GitHub Models（`GITHUB_TOKEN` 白嫖通道）已于 2026-07-30 被 GitHub 永久退役，不可再用。

| Secret | 免费额度 | 备注 |
|---|---|---|
| `OPENAI_BASE_URL` + `CUSTOM_API_KEY` + `LLM_MODEL` | 智谱 GLM Flash 系列**完全免费** | **国内首选**：[open.bigmodel.cn](https://open.bigmodel.cn) 手机号注册 → API Keys → 新建。三个 Secret 分别填 `https://open.bigmodel.cn/api/paas/v4`、你的 key、`glm-4.7-flash`（200K 上下文；也可用 `glm-4.5-flash`） |
| `GEMINI_API_KEY` | gemini-2.5-flash 免费档 | [aistudio.google.com](https://aistudio.google.com/apikey)，需支持地区访问 |
| `GROQ_API_KEY` | gpt-oss 等免费档，含 Whisper 转录 | [console.groq.com](https://console.groq.com/keys)，部分地区/IP 注册会被拦 |

> 任何 OpenAI 兼容的服务（DeepSeek、硅基流动、OpenRouter 等）都能接：填 `OPENAI_BASE_URL` + `CUSTOM_API_KEY` + `LLM_MODEL` 三件套即可。

**可选**：

| Secret | 用途 |
|---|---|
| `YT_COOKIES_B64` | YouTube 拦字幕时才需要。本地 `base64 -i cookies.txt \| pbcopy` 后粘进来 |

### 3. 跑一次验证

仓库 → **Actions → DOAC Digest → Run workflow**。

- 首次运行会先把历史集全部标记为已读，**只推送最新 1 集**，不会一次性轰炸你的邮箱。
- 之后每天 UTC 01:00 / 13:00（北京时间 09:00 / 21:00）自动跑，有新集才发信。

---

## 本地调试

```bash
pip install -r requirements.txt

# 只看会处理哪些集（不调 LLM、不发信）
python3 doac_digest.py --dry-run

# 真跑一次，但只打印不发信
export GROQ_API_KEY=... && python3 doac_digest.py --no-mail --max-new 1

# 强制重跑某一集（guid 用 --dry-run 看不到，从 state/seen.json 或 RSS 里取）
python3 doac_digest.py --force "<guid>"
```

想调摘要风格，改 `doac_digest.py` 里的 `REDUCE_PROMPT`（比如加"用更口语的方式""只保留与创业/健康相关的内容"）。

---

## 已知坑

1. **"Most Replayed Moment" 这类剪辑片段默认跳过**（标题关键词 + 时长 < 30 分钟双重过滤），只推正片。想连片段一起收，把 `MIN_SECS` 设为 `0` 并改 `SKIP_KEYWORDS`。
2. **免费额度有限**：单集约 15 万字 → 切成 6 段，共 7 次 LLM 调用，一天几集绰绰有余。若同时跑多个播客，注意 Groq/Gemini 的 RPM 限制（脚本每次调用间隔 5 秒）。
3. **Gmail 必须用应用专用密码**（且开两步验证）；**163/QQ 必须用授权码**——填登录密码必然认证失败。163 只支持 465 SSL 端口，脚本已自动适配。
4. **首次运行建议用 `--dry-run` 看一眼**，确认 RSS 解析正常再正式跑。
5. **旧集可能没有官方 transcript**（153 集以前），此时会走 YouTube 字幕；yt-dlp 现在常被反爬拦，必要时配 `YT_COOKIES_B64`。新集不受影响。

## 可调参数

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `SINCE_DAYS` | `7` | 只处理最近 N 天发布的集（防止把 886 集历史当新集） |
| `MIN_SECS` | `1800` | 过滤掉短于此秒数的条目；设 `0` 关闭 |
| `MAX_CHARS` | `250000` | 单集文本上限，防止超长集吃光额度 |
| `LLM_PROVIDER` / `LLM_MODEL` | 自动 | 强制指定 `gemini` / `groq` / `github` 和模型名 |
| `DOAC_RSS` | DOAC 官方源 | 换成别的播客 RSS 即可复用 |

## 想加别的播客？

`doac_digest.py` 顶部改 `RSS_URL` 即可；YouTube 搜索关键词在 `find_youtube_id()` 里，把 `Steven Bartlett` 换成对应的节目名/主持人。
