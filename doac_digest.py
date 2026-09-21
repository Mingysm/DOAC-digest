#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DOAC (The Diary Of A CEO) 播客自动更新 + 内容摘要 + 邮件推送

流程:
  1. 拉取官方 RSS，与 state/seen.json 比对，只处理新集
  2. 取文本稿: 优先抓 YouTube 自动字幕(json3)；失败则下载音频用 Whisper 转录兜底
  3. 调用免费 LLM 做 map-reduce 摘要，输出中文
  4. 通过 SMTP 发送 HTML 邮件

免费 LLM 支持(自动按可用 key 选择):
  - gemini  : Google AI Studio 免费档 (GEMINI_API_KEY)
  - groq    : Groq 免费档 (GROQ_API_KEY) + Whisper 转录兜底
  - github  : GitHub Models 免费档 (GITHUB_TOKEN，Actions 里自带)

用法:
  python3 doac_digest.py --dry-run            # 只看会处理哪些集，不调 LLM、不发信
  python3 doac_digest.py --max-new 2          # 本次最多处理 2 集
  python3 doac_digest.py --force GUID         # 强制重跑某一集(调试用)
"""

import argparse
import json
import os
import re
import smtplib
import subprocess
import sys
import tempfile
import textwrap
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import Request, urlopen

import requests

RSS_URL = os.environ.get("DOAC_RSS", "https://rss2.flightcast.com/xmsftuzjjykcmqwolaqn6mdn")
STATE_FILE = os.environ.get("DOAC_STATE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "seen.json"))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"

SUMMARY_LANG = os.environ.get("SUMMARY_LANG", "zh")  # zh / en

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 1. RSS 解析
# ---------------------------------------------------------------------------
NS = {
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "podcast": "https://podcastindex.org/namespace/1.0",
}


def fetch_rss(url=RSS_URL):
    local = os.environ.get("DOAC_RSS_FILE")
    if local and os.path.exists(local):  # 本地调试用，避免每次重下 6MB
        return open(local, "rb").read()
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=300) as r:
        return r.read()


def parse_rss(raw):
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        def txt(tag, ns=None):
            el = it.find(tag, ns) if ns else it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        title = txt("title")
        guid = txt("guid") or txt("link") or title
        enclosure = it.find("enclosure")
        audio = enclosure.get("url") if enclosure is not None else ""
        duration = txt("itunes:duration", NS)
        # DOAC 官方 RSS 直接带 transcript（最近 150+ 集都有），这是最稳最省事的文本源
        tr = it.find("podcast:transcript", NS)
        transcript_url = (tr.get("url") or "") if tr is not None else ""
        desc = txt("description") or txt("content:encoded", NS)
        link = txt("link")

        pub = txt("pubDate")
        pub_dt = None
        try:
            from email.utils import parsedate_to_datetime
            pub_dt = parsedate_to_datetime(pub).astimezone(timezone.utc)
            pub_iso = pub_dt.isoformat()
        except Exception:
            pub_iso = pub

        # itunes:duration 可能是 "1:02:03" 或纯秒数
        secs = 0
        if duration:
            if ":" in duration:
                try:
                    secs = sum(int(a) * b for a, b in zip(reversed(duration.split(":")), (1, 60, 3600)))
                except ValueError:
                    secs = 0
            else:
                try:
                    secs = int(float(duration))
                except ValueError:
                    secs = 0

        items.append({
            "guid": guid,
            "title": title,
            "link": link,
            "audio": audio,
            "transcript_url": transcript_url,
            "duration": duration,
            "secs": secs,
            "pubDate": pub_iso,
            "dt": pub_dt,
            "description": re.sub(r"<[^>]+>", " ", desc)[:1000],
        })
    return items


# DOAC 的 RSS 里混有 "Most Replayed Moment" 之类的剪辑片段，默认跳过
SKIP_KEYWORDS = ("most replayed", "trailer", "preview", "teaser", "coming soon", "introducing")
MIN_SECS = int(os.environ.get("MIN_SECS") or 1800)  # 默认过滤掉 < 30 分钟的条目


def should_skip(ep):
    low = ep["title"].lower()
    for k in SKIP_KEYWORDS:
        if k in low:
            return f"标题含 '{k}'"
    if MIN_SECS and 0 < ep["secs"] < MIN_SECS:
        return f"时长 {ep['secs']}s < {MIN_SECS}s"
    return ""


# ---------------------------------------------------------------------------
# 2. 获取文本稿
# ---------------------------------------------------------------------------
def fetch_text(url):
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=120) as r:
        return r.read().decode("utf-8", "ignore")


def _run(cmd, timeout=600):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "command not found: %s" % cmd[0]


def find_youtube_id(title):
    """用标题搜 YouTube，返回最匹配的 video id"""
    query = re.sub(r"[^\w\s\-:]", " ", title)[:70] + " Steven Bartlett"
    rc, out, err = _run([
        "yt-dlp", "--flat-playlist", "--no-warnings", "--no-playlist",
        "--print", "%(id)s\t%(title)s",
        f"ytsearch5:{query}",
    ], timeout=180)
    if rc != 0 or not out.strip():
        log(f"  YouTube 搜索失败 rc={rc} {err[:120]}")
        return None

    def norm(s):
        return set(re.findall(r"[a-z0-9]+", s.lower()))

    tset = norm(title)
    best, best_score = None, 0.0
    for line in out.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        vid, vtitle = parts[0].strip(), parts[1].strip()
        score = len(tset & norm(vtitle)) / max(1, len(tset | norm(vtitle)))
        if score > best_score:
            best, best_score = vid, score
    log(f"  YouTube 匹配: {best} (相似度 {best_score:.2f})")
    return best if best_score >= 0.25 else None


def clean_json3(path):
    """解析 YouTube json3 字幕 -> 纯文本。json3 的 event 通常就是完整一句，重复很少"""
    data = json.loads(open(path, encoding="utf-8").read())
    lines, buf = [], []
    for ev in data.get("events", []):
        segs = ev.get("segs") or []
        s = "".join(seg.get("utf8", "") for seg in segs)
        s = s.replace("\n", " ").strip()
        if not s:
            continue
        buf.append(s)
        # 遇到句末标点就断行，保持可读性
        if s[-1] in ".!?" or len(buf) >= 6:
            line = " ".join(buf)
            if line not in lines[-3:]:  # 去掉滚动字幕的相邻重复
                lines.append(line)
            buf = []
    if buf:
        lines.append(" ".join(buf))
    return "\n".join(lines)


def clean_vtt(path):
    """兜底: 解析 vtt/srt，做相邻重复行去重"""
    raw = open(path, encoding="utf-8", errors="ignore").read()
    raw = re.sub(r"^WEBVTT.*?\n\n", "", raw, flags=re.S)
    raw = re.sub(r"^\d{2}:\d{2}:\d{2}\.\d{3}.*?$", "", raw, flags=re.M)
    raw = re.sub(r"<[^>]+>", "", raw)
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.isdigit():
            continue
        if "-->" in line:
            continue
        if out and (line == out[-1] or (len(out) > 1 and line == out[-2])):
            continue
        out.append(line)
    return "\n".join(out)


def get_transcript(ep):
    """文本稿三级降级：官方 RSS transcript → YouTube 字幕 → 音频 Whisper 转录"""
    if ep.get("transcript_url"):
        log("  来源 1: 官方 RSS transcript")
        try:
            raw = fetch_text(ep["transcript_url"])
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "t.vtt")
                open(p, "w", encoding="utf-8").write(raw)
                text = clean_vtt(p)
            if len(text) > 2000:
                log(f"  transcript OK: {len(text)} 字符")
                return text, ep["link"] or ep["transcript_url"]
        except Exception as e:
            log(f"  官方 transcript 失败: {e}")

    log("  来源 2: YouTube 字幕")
    text, src = get_transcript_from_youtube(ep["title"])
    if text:
        return text, src

    log("  来源 3: 下载音频 + Whisper 转录")
    text = transcribe_audio(ep["audio"])
    return (text, ep["link"]) if text else (None, None)


def get_transcript_from_youtube(title):
    vid = find_youtube_id(title)
    if not vid:
        return None, None
    with tempfile.TemporaryDirectory() as td:
        cmd = [
            "yt-dlp", "--no-warnings", "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", "en.*,en", "--sub-format", "json3/vtt/best",
            "-o", os.path.join(td, "sub.%(ext)s"),
            "--extractor-args", "youtube:player_client=mweb,web_safari",
            f"https://www.youtube.com/watch?v={vid}",
        ]
        cookies = os.environ.get("YT_COOKIES_B64")
        if cookies:
            cookie_path = os.path.join(td, "cookies.txt")
            import base64
            open(cookie_path, "w").write(base64.b64decode(cookies).decode("utf-8", "ignore"))
            cmd += ["--cookies", cookie_path]
        rc, out, err = _run(cmd, timeout=600)
        if rc != 0:
            log(f"  字幕下载失败 rc={rc}: {err[:160]}")
            return None, None
        files = sorted(os.listdir(td))
        for f in files:
            p = os.path.join(td, f)
            if f.endswith(".json3"):
                text = clean_json3(p)
            elif f.endswith((".vtt", ".srt")):
                text = clean_vtt(p)
            else:
                continue
            if len(text) > 2000:
                log(f"  字幕获取成功: {len(text)} 字符 ({f})")
                return text, f"https://www.youtube.com/watch?v={vid}"
    log("  未拿到可用字幕")
    return None, None


def transcribe_audio(audio_url):
    """兜底: 下载音频 -> ffmpeg 压成低码率 mono -> Groq Whisper 转录"""
    key = os.environ.get("GROQ_API_KEY")
    if not key or not audio_url:
        log("  无 GROQ_API_KEY，跳过音频转录兜底")
        return None
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "src.mp3")
        log("  下载音频...")
        try:
            with requests.get(audio_url, headers={"User-Agent": UA}, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(src, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
        except Exception as e:
            log(f"  音频下载失败: {e}")
            return None
        # 16kbps mono 16kHz: 1.5 小时约 11MB，可单次上传
        small = os.path.join(td, "small.mp3")
        rc, _, err = _run(["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000",
                           "-b:a", "16k", small], timeout=1800)
        if rc != 0:
            log(f"  ffmpeg 失败: {err[:160]}")
            return None
        size_mb = os.path.getsize(small) / 1e6
        log(f"  压缩后 {size_mb:.1f}MB, 调用 Whisper...")
        segs = []
        # Groq 单次 25MB 限制，超过则按时长切片
        if size_mb <= 24:
            parts = [small]
        else:
            n = int(size_mb // 20) + 1
            dur_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=nw=1:nk=1", small]
            _, dout, _ = _run(dur_cmd)
            total = float(dout.strip() or 0)
            chunk = total / n
            parts = []
            for i in range(n):
                p = os.path.join(td, f"part{i}.mp3")
                _run(["ffmpeg", "-y", "-i", small, "-ss", str(i * chunk),
                      "-t", str(chunk), "-c", "copy", p], timeout=600)
                parts.append(p)
        for p in parts:
            try:
                with open(p, "rb") as f:
                    r = requests.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"},
                        files={"file": (os.path.basename(p), f, "audio/mpeg")},
                        data={"model": "whisper-large-v3", "response_format": "text"},
                        timeout=900,
                    )
                if r.status_code == 200:
                    segs.append(r.text.strip())
                else:
                    log(f"  Whisper 失败 {r.status_code}: {r.text[:200]}")
                    return None
            except Exception as e:
                log(f"  Whisper 异常: {e}")
                return None
        text = "\n".join(segs)
        log(f"  转录完成: {len(text)} 字符")
        return text or None


# ---------------------------------------------------------------------------
# 3. LLM
# ---------------------------------------------------------------------------
MAP_PROMPT = """你是播客内容分析助手。下面是《The Diary Of A CEO》某一集文字稿的【第 {i}/{n} 部分】。

请提取这部分的关键信息，用中文输出（专有名词保留英文）：
1. 讨论的核心话题（1-3 条，每条一句话）
2. 具体的事实、数据、研究结论、案例（保留数字）
3. 给出的可执行建议或方法论
4. 值得摘录的金句（英文原文 + 中文翻译）

要求：只总结这部分内容，不要臆造；用 bullet list；控制在 400 字以内。

标题: {title}
文字稿:
{chunk}"""

REDUCE_PROMPT = """下面是《The Diary Of A CEO》一集的分段摘要，请把它们整合为一份完整、易读的中文笔记。

STRUCTURE（严格按此 Markdown 结构输出）:
## 一句话总结
（一句话说清这期在讲什么）

## 嘉宾
（姓名 + 身份/代表作）

## 核心观点
（5-8 条，每条 1-2 句，说清"是什么 + 为什么"）

## 关键事实与数据
（带数字的具体事实、研究、案例）

## 可执行建议
（听众能直接照做的 3-6 条）

## 金句
（3-5 条，英文原文 + 中文翻译）

## 延伸思考
（1-2 条批判性提示或争议点，可写"无"）

要求：不要臆造文字稿里没有的内容；不要写"本部分/第一段"这类分段痕迹。

标题: {title}
嘉宾背景: {guest}
分段摘要:
{chunks}"""


class LLM:
    def __init__(self):
        provider = os.environ.get("LLM_PROVIDER", "").lower()
        gemini, groq, gh = os.environ.get("GEMINI_API_KEY"), os.environ.get("GROQ_API_KEY"), os.environ.get("GITHUB_TOKEN")
        base = (os.environ.get("OPENAI_BASE_URL") or "").strip()
        if not provider:
            # 任何 OpenAI 兼容服务（OpenRouter / DeepSeek / 智谱 / SiliconFlow 等）
            # 设 OPENAI_BASE_URL + CUSTOM_API_KEY + LLM_MODEL 即可接入
            provider = "custom" if base else "gemini" if gemini else "groq" if groq else "github" if gh else ""
        if provider == "gemini" and not gemini:
            provider = ""
        self.provider = provider
        self.model = os.environ.get("LLM_MODEL", "")
        if not self.model:
            self.model = {"gemini": "gemini-2.5-flash",
                          "groq": "llama-3.3-70b-versatile",
                          "github": "openai/gpt-4o-mini"}.get(provider, "")
        self.key = {"gemini": gemini, "groq": groq, "github": gh,
                    "custom": os.environ.get("CUSTOM_API_KEY") or os.environ.get("OPENAI_API_KEY")}.get(provider)
        self.base = base
        if provider == "github":
            log("警告: GitHub Models 已于 2026-07-30 永久退役(410)，建议改配智谱等 OpenAI 兼容服务: "
                "OPENAI_BASE_URL + CUSTOM_API_KEY + LLM_MODEL，或 GEMINI_API_KEY / GROQ_API_KEY")
        if not self.provider or (provider == "custom" and not self.model):
            raise SystemExit(
                "未配置可用的 LLM，三选一（仓库 Settings → Secrets → Actions）：\n"
                "  1) 智谱(推荐,大陆直连): OPENAI_BASE_URL=https://open.bigmodel.cn/api/paas/v4 "
                "+ CUSTOM_API_KEY=<你的key>，模型默认 glm-4.7-flash\n"
                "  2) GEMINI_API_KEY=<key>  (aistudio.google.com)\n"
                "  3) GROQ_API_KEY=<key>   (console.groq.com)\n"
                "注意: GitHub Models 已于 2026-07 永久退役，GITHUB_TOKEN 通道不可用"
            )
        if not self.provider:
            raise SystemExit("未找到可用 LLM: 请设置 GEMINI_API_KEY / GROQ_API_KEY / GITHUB_TOKEN 之一")
        log(f"LLM: {self.provider} / {self.model}")

    def chat(self, prompt, max_tokens=4000, retries=5):
        for attempt in range(retries):
            try:
                if self.provider == "gemini":
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.key}"
                    body = {"contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.3}}
                    r = requests.post(url, json=body, timeout=300)
                    if r.status_code != 200:
                        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
                    return r.json()["candidates"][0]["content"]["parts"][0]["text"]
                else:  # groq / github / custom 都是 OpenAI 兼容
                    url = {"groq": "https://api.groq.com/openai/v1/chat/completions",
                           "github": "https://models.github.ai/inference/chat/completions"}.get(
                              self.provider, self.base.rstrip("/") + "/chat/completions")
                    headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
                    body = {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.3, "max_tokens": max_tokens}
                    r = requests.post(url, json=body, headers=headers, timeout=300)
                    if r.status_code != 200:
                        raise RuntimeError(f"{r.status_code} {r.text[:200]}")
                    return r.json()["choices"][0]["message"]["content"]
            except Exception as e:
                msg = str(e)
                if "410" in msg or "retirement" in msg:  # 服务永久下线，重试没有意义
                    log("  该 LLM 服务已永久下线(410)，不再重试")
                    raise
                if "429" in msg or "1305" in msg or "访问量过大" in msg:
                    wait = 20 * (attempt + 1)  # 免费模型高峰过载：20/40/60/80s 递增等待
                    log(f"  LLM 过载(429)，等待 {wait}s 后重试({attempt + 1}/{retries})，可稍后再跑或换 LLM_MODEL")
                    time.sleep(wait)
                    continue
                log(f"  LLM 调用失败({attempt + 1}/{retries}): {e}")
                if attempt == retries - 1:
                    raise
                time.sleep(5 * (attempt + 1))


def chunk_text(text, size=30000):
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 > size and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n" + p).strip()
    if cur:
        chunks.append(cur)
    return chunks


def summarize(llm, ep, text):
    max_chars = int(os.environ.get("MAX_CHARS") or 250000)
    if len(text) > max_chars:  # 超长集截断，避免免费额度被单集吃光
        log(f"  文本 {len(text)} 字符，截断到 {max_chars}")
        text = text[:max_chars]
    chunks = chunk_text(text)
    log(f"  分段: {len(chunks)} 块")
    partials = []
    for i, c in enumerate(chunks, 1):
        log(f"  map {i}/{len(chunks)}")
        partials.append(llm.chat(MAP_PROMPT.format(i=i, n=len(chunks), title=ep["title"], chunk=c), max_tokens=2000))
        time.sleep(5)  # 免费档 RPM 通常 15，放慢一点避免 429
    joined = "\n\n".join(f"[Part {i}]\n{p}" for i, p in enumerate(partials, 1))
    log("  reduce")
    return llm.chat(REDUCE_PROMPT.format(title=ep["title"], guest=ep["description"][:400], chunks=joined), max_tokens=4000)


# ---------------------------------------------------------------------------
# 4. 邮件
# ---------------------------------------------------------------------------
def md_to_html(md):
    html_body, in_list = [], False
    for line in md.splitlines():
        s = line.rstrip()
        if re.match(r"^#{1,6}\s", s):
            if in_list:
                html_body.append("</ul>")
                in_list = False
            lvl = len(s) - len(s.lstrip("#"))
            html_body.append(f"<h{min(lvl + 1, 4)}>{s.lstrip('#').strip()}</h{min(lvl + 1, 4)}>")
        elif re.match(r"^\s*[-*]\s", s):
            if not in_list:
                html_body.append("<ul>")
                in_list = True
            item_txt = re.sub(r"^\s*[-*]\s", "", s)
            html_body.append(f"<li>{item_txt}</li>")
        elif s.strip():
            if in_list:
                html_body.append("</ul>")
                in_list = False
            html_body.append(f"<p>{s}</p>")
    if in_list:
        html_body.append("</ul>")
    body = "\n".join(html_body)
    body = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", body)
    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;
font-size:15px;line-height:1.75;color:#1a1a1a;max-width:680px;margin:0 auto;padding:16px">
{body}</div>"""


def send_mail(subject, html_body, plain):
    host = os.environ.get("SMTP_HOST")
    port = int((os.environ.get("SMTP_PORT") or "").strip() or 587)
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO") or user
    if not (host and user and pwd and to):
        log("SMTP 未配置，跳过发信")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # 465 走 SSL（网易 163/QQ 等只支持 465，不支持 587 STARTTLS）；587 走 STARTTLS（Gmail 等）
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=60) as s:
            s.login(user, pwd)
            s.sendmail(user, [t.strip() for t in to.split(",")], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls()
            s.login(user, pwd)
            s.sendmail(user, [t.strip() for t in to.split(",")], msg.as_string())
    log(f"  邮件已发送 -> {to}")
    return True


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {"seen": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    seen, out = set(), []
    for g in state["seen"]:  # seen 按"最新在前"排列，截断时保留最新的
        if g not in seen:
            seen.add(g)
            out.append(g)
    state["seen"] = out[:500]
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列出会处理的集，不调 LLM、不发信")
    ap.add_argument("--max-new", type=int, default=2, help="单次最多处理几集")
    ap.add_argument("--since-days", type=int, default=int(os.environ.get("SINCE_DAYS") or 7),
                    help="只处理最近 N 天发布的集（默认 7）")
    ap.add_argument("--force", default="", help="强制处理指定 guid")
    ap.add_argument("--no-mail", action="store_true")
    args = ap.parse_args()

    log(f"拉取 RSS: {RSS_URL}")
    items = parse_rss(fetch_rss())
    log(f"RSS 共 {len(items)} 集")

    state = load_state()
    seen = set(state["seen"])

    # 只关心最近 since_days 天内发布的集：这样 886 集历史根本不进候选，
    # 不必维护一个几百条、还会被截断的已读列表
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.since_days)
    candidates = []
    for it in items:
        if it["dt"] is None or it["dt"] < cutoff:
            continue
        reason = should_skip(it)
        if reason:
            log(f"  跳过《{it['title'][:55]}》: {reason}")
            continue
        candidates.append(it)
    log(f"最近 {args.since_days} 天内符合条件的集: {len(candidates)}")

    if args.force:
        pending = [it for it in items if it["guid"] == args.force]
    elif not seen:  # 首次运行：候选全部标记已读，只推送最新 1 集，避免轰炸邮箱
        log("首次运行：只推送最新 1 集，其余候选标记为已读")
        for it in candidates:
            state["seen"].insert(0, it["guid"])
        pending = candidates[:1]
        state["seen"] = [g for g in state["seen"] if g not in {p["guid"] for p in pending}]
        save_state(state)
        seen = set(state["seen"])
    else:
        pending = [it for it in candidates if it["guid"] not in seen][: args.max_new]

    if not pending:
        log("没有新集，结束")
        save_state(state)
        return 0

    log(f"待处理 {len(pending)} 集:")
    for it in pending:
        log(f"  - {it['title'][:80]} ({it['pubDate'][:10]})")

    if args.dry_run:
        log("dry-run 模式，不调用 LLM")
        return 0

    llm = LLM()
    ok = 0
    for ep in pending:
        log(f"\n=== {ep['title'][:80]} ===")
        text, source_url = get_transcript(ep)
        if not text or len(text) < 1000:
            log("  获取文本稿失败，跳过")
            continue

        try:
            md = summarize(llm, ep, text)
        except Exception as e:
            log(f"  摘要失败: {e}")
            continue

        subject = f"[DOAC] {ep['title'][:60]}"
        header = (f"<p style='color:#666;font-size:13px'>"
                  f"发布: {ep['pubDate'][:10]} · 时长: {ep['duration'] or '-'} · "
                  f"来源: <a href='{source_url}'>{'YouTube' if src else 'Podcast'}</a></p><hr>")
        plain = f"{ep['title']}\n{ep['pubDate'][:10]}\n{source_url}\n\n{md}"

        if not args.no_mail:
            send_mail(subject, header + md_to_html(md), plain)

        ok += 1
        state["seen"].insert(0, ep["guid"])
        save_state(state)
        time.sleep(3)

    log(f"\n完成: {ok}/{len(pending)} 集")
    save_state(state)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
