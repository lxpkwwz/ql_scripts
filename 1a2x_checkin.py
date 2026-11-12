#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1A2X 社区（https://1a2x.com）每日签到脚本 —— 青龙面板

功能：
  1. 调用 /growth/checkin 完成每日签到；优先从香蕉流水判断今日是否已签到及实际 +N 香蕉
  2. 自动回复官方水楼，每条回复 +1 香蕉，每次运行最多 10 条
     （回复内容默认取自每日一言，取不到时使用内置笑话）
  3. 回复前统计今日『有效回帖』条数（优先香蕉流水，读不到时退回个人主页）
  4. 每条回复后核对香蕉流水是否新增『有效回帖 +1』，未增加则停止后续回复

站点规则适配：
  - 回帖正文至少 10 个文字或数字，过短的内容自动跳过
  - 每日按新加坡时间（UTC+8）结算，与站点一致
  - 自动跳过今日已发过的相同内容（读取个人主页记录，不落盘）
  - 达到每日奖励次数后不再多发

环境变量：
  A2X_COOKIE        必填。登录 1a2x.com 后从浏览器复制的完整 Cookie，例如：
                    server_session_xxxxxxxx=xxxx; __Host-nc.csrf=xxxx
  A2X_DOMAIN        可选。默认 https://1a2x.com
  A2X_REPLY         可选。是否自动回复，默认 1；设为 0 关闭
  A2X_REPLY_TOPIC   可选。回复的帖子 ID，默认 102（官方指定水楼），多个用逗号分隔
  A2X_REPLY_COUNT   可选。每次运行回复条数，默认 10，最大 10
  A2X_REPLY_DELAY   可选。每条回复间隔秒数，默认 10；触发 429 时会自动退避重试
  A2X_REPLY_SOURCE  可选。回复内容来源：hitokoto（默认，每日一言）或 joke（内置笑话）
  A2X_HITOKOTO_API  可选。每日一言接口，默认 https://v1.hitokoto.cn/
  A2X_VERIFY_LEDGER 可选。是否用香蕉流水逐条校验，默认 1；设为 0 关闭

多账号：
  A2X_COOKIE 中多个账号的 Cookie 用换行（或 &）分隔。

依赖：
  requests（青龙面板一般已内置，缺失时执行 pip3 install requests）

通知：
  使用青龙面板内置通知模块 notify.py，需先在「通知设置」中配置好通知渠道。
  会推送：签到成功/失败汇总（含回复结果）、未配置 Cookie、脚本运行异常。

定时规则示例：
  17 8 * * *
"""

import datetime
import html as html_lib
import importlib.util
import json
import os
import random
import re
import sys
import time
import traceback
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    print("缺少 requests 库，请先执行: pip3 install requests")
    sys.exit(1)

DOMAIN = (os.environ.get("A2X_DOMAIN") or "https://1a2x.com").rstrip("/")
COOKIE_ENV = os.environ.get("A2X_COOKIE") or os.environ.get("A2X_COOKIES") or ""
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SESSION_COOKIE_PREFIX = "server_session_"
CSRF_COOKIE = "__Host-nc.csrf"


def env_int(name, default):
    try:
        return int((os.environ.get(name) or "").strip())
    except Exception:
        return default


def env_float(name, default):
    try:
        return float((os.environ.get(name) or "").strip())
    except Exception:
        return default


REPLY_ENABLED = (os.environ.get("A2X_REPLY") or "1").strip().lower() not in ("0", "false", "off", "no")
REPLY_COUNT = max(0, min(10, env_int("A2X_REPLY_COUNT", 10)))
REPLY_DELAY = max(1.0, env_float("A2X_REPLY_DELAY", 10.0))
REPLY_TOPICS = [
    int(x) for x in re.split(r"[,，;；\s]+", os.environ.get("A2X_REPLY_TOPIC") or "102") if x.isdigit()
] or [102]
REPLY_SOURCE = (os.environ.get("A2X_REPLY_SOURCE") or "hitokoto").strip().lower()
HITOKOTO_API = (os.environ.get("A2X_HITOKOTO_API") or "https://v1.hitokoto.cn/").strip()
VERIFY_LEDGER = (os.environ.get("A2X_VERIFY_LEDGER") or "1").strip().lower() not in ("0", "false", "off", "no")

JOKES = [
    "为什么程序员总是分不清万圣节和圣诞节？因为 Oct 31 == Dec 25。",
    "我对电脑说“我爱你”，它回我“404 Not Found”。",
    "减肥第一天，我决定只吃水果。结果吃了两斤葡萄，因为葡萄是水果。",
    "老板问我为什么迟到，我说路上堵车。他说你骑共享单车堵什么车？我说堵的是心。",
    "有人问我存款多少，我说三个亿：一个回忆，一个失忆，一个不容易。",
    "我妈说我房间像猪窝，我说那你就是猪妈妈。然后我挨打了。",
    "今天去健身，教练问我目标是什么，我说来都来了，先办张卡吧。",
    "为什么数学书总是很忧郁？因为它有太多的问题。",
    "一只乌鸦口渴了，看见一瓶水，于是它把瓶子推倒了——它想喝地上的。",
    "我问语音助手我帅不帅，它说“正在为你搜索附近的镜子”。",
    "小时候以为钱是万能的，长大后发现，没钱是万万不能的。",
    "我发誓要早睡，结果刷手机刷到凌晨两点，手机也发誓要陪我。",
    "同事问我为什么总带饭，我说为了省钱。他说你真会过日子。我说不，我是真没钱。",
    "为什么海水是咸的？因为鱼在里面流泪。为什么鱼在流泪？因为它们在打工。",
    "今天面试官问我有什么特长，我说我特能加班。他说我们不需要加班。我说那我特能摸鱼。",
    "我家猫每天准点叫我起床，比我闹钟还准。后来我发现，它只是饿了。",
    "医生说我要多运动，于是我每天从床上运动到沙发上。",
    "为什么程序员喜欢黑咖啡？因为 Java 需要编译，人生需要续命。",
    "朋友问我最近怎么样，我说在减肥。他说你瘦了吗？我说我在减，还没到。",
    "我发现一个规律：只要我一开始学习，桌子就特别需要收拾。",
    "今天天气真好，适合睡觉。昨天天气不好，也适合睡觉。",
    "我问老板能不能涨工资，他说公司现在困难。我说那我能不能困难一下？他说你已经在困难了。",
    "为什么鱼不上岸？因为岸上有猫，还有房贷。",
    "我买了一个智能音箱，它最智能的地方就是在我唱歌时自动静音。",
    "人生就像心电图，一帆风顺说明你挂了。",
    "我妈让我多喝水，我听话地多喝了两杯奶茶。",
    "为什么有些人走路带风？因为他们吃了太多洋葱。",
    "今天去理发，理发师问我想剪什么发型，我说显年轻一点。他把我头发剪短了，说：现在你看起来像个学生。我说我本来就是学生。他说那当我没说。",
    "我在群里发了个红包，抢到最多的人是我妈。我问她怎么抢这么快，她说她一直盯着。",
    "为什么程序员总是很冷静？因为他们已经调试到麻木了。",
    "今天坐地铁，一个大爷问我几点了，我说八点半。他说谢谢，然后继续睡。我怀疑他根本不想知道。",
    "我家楼下新开了一家健身房，开业三天就倒闭了，因为大家都只路过不进去。",
    "小时候我以为长大了就能为所欲为，长大后发现，是老板为所欲为。",
    "为什么书到用时方恨少？因为买的时候只看了封面。",
    "今天决定戒掉奶茶，结果路过奶茶店，发现它在搞活动。天意如此。",
    "朋友说我像一只猫，我问为什么，他说因为我白天睡觉晚上精神。",
    "我问智能助手我是不是天才，它说“正在为你搜索天才的定义”。",
    "为什么月亮晚上才出来？因为它也要睡觉，白天起不来。",
    "今天上班迟到了，我跟老板说闹钟没响，他说你的闹钟是不是也叫“再睡五分钟”？",
    "我的人生目标是财务自由，目前进度：自由还在，财务没了。",
    "为什么企鹅不怕冷？因为它穿着天然的羽绒服，还不用担心洗。",
    "今天称体重，秤显示“请一个一个上”。我决定换个体重秤。",
    "有人问我为什么不谈恋爱，我说因为爱情太贵，我选择贫穷。",
    "为什么程序员喜欢用黑暗模式？因为这样可以假装自己很酷，还能省电。",
    "我去超市买鸡蛋，售货员问我要不要袋子，我说不用，我抱着回去。然后我摔了。",
    "为什么电脑总是蓝屏？因为它也想看看天空。",
    "今天爬山，朋友说山顶风景真好。我说是啊，但我更想知道下山的路在哪。",
    "我妈问我以后想干什么，我说想躺平。她说你先把碗洗了再躺。",
    "为什么鱼记性只有七秒？因为它不想记住昨天的烦恼。",
    "今天在地铁上看到一个人对着手机傻笑，我凑近一看，原来他在看自己昨天发的朋友圈。",
]


def log(msg):
    print("[{}] {}".format(time.strftime("%H:%M:%S"), msg), flush=True)


def new_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    return s


def find_session_cookie_name():
    """未提供 cookie 名时，通过一次请求探测服务端的 session cookie 名。"""
    s = new_session()
    try:
        s.get(DOMAIN + "/", timeout=20)
    except Exception:
        return None
    for c in s.cookies:
        if c.name.startswith(SESSION_COOKIE_PREFIX):
            return c.name
    return None


def load_cookies(sess, raw):
    raw = raw.strip()
    if "=" in raw:
        for seg in raw.split(";"):
            seg = seg.strip()
            if not seg or "=" not in seg:
                continue
            k, v = seg.split("=", 1)
            k, v = k.strip(), v.strip()
            # CSRF Cookie 由脚本每次请求时重新获取，避免过期 Cookie 重复导致校验失败
            if k == CSRF_COOKIE:
                continue
            sess.cookies.set(k, v)
        return
    # 只粘贴了 session 值的情况
    name = find_session_cookie_name()
    if not name:
        raise RuntimeError(
            "无法识别 Cookie，请复制完整 Cookie（形如 server_session_xxx=...; __Host-nc.csrf=...）"
        )
    sess.cookies.set(name, raw)


def get_csrf_token(sess):
    try:
        r = sess.get(DOMAIN + "/", timeout=20)
    except Exception as e:
        raise RuntimeError("访问站点失败: {}".format(e))
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', r.text)
    if m:
        return m.group(1)
    cookie = sess.cookies.get(CSRF_COOKIE)
    if cookie:
        return cookie.split(".")[0]
    raise RuntimeError("未获取到 CSRF Token，Cookie 可能不完整")


def is_already_checked(text):
    keywords = ("已签到", "已经签到", "重复签到", "already checked", "checked in today")
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def strip_tags(fragment):
    fragment = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def extract_flash(page_html):
    """提取页面上的 flash / toast 提示文本。"""
    pattern = re.compile(
        r'<[a-zA-Z0-9]+[^>]*class="[^"]*\b(?:flash|toast)\b[^"]*"[^>]*>(.*?)</[a-zA-Z0-9]+>',
        re.S | re.I,
    )
    messages = []
    for m in pattern.finditer(page_html):
        text = strip_tags(m.group(1))
        if text and text not in messages:
            messages.append(text)
    return messages


def judge_growth_page(page_html):
    """根据签到后 /growth 页面上的提示判断结果，返回 (ok, msg) 或 (None, None)。"""
    flashes = extract_flash(page_html)
    page_text = strip_tags(page_html)
    blob = " ".join(flashes)

    if blob:
        if any(k in blob for k in ("失败", "错误", "无效", "过期", "请先登录", "请登录", "频繁", "禁止", "不能")):
            return False, "接口提示: {}".format(blob)
        return True, "接口提示: {}".format(blob)

    if any(k in page_text for k in ("今日已签到", "已经签到", "明天再来", "已连续签到")):
        return True, "今日已签到"
    if "签到成功" in page_text:
        return True, "签到成功"

    idx = page_text.find("签到")
    if idx >= 0:
        log("页面签到相关文本: {}".format(page_text[max(0, idx - 60): idx + 120]))
    return None, None


def csrf_from_html(page_html):
    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', page_html or "")
    return m.group(1) if m else None


def page_says_signed(page_html):
    text = strip_tags(page_html or "")
    return any(k in text for k in ("今日已签到", "已经签到", "明天再来"))


def to_int(value):
    try:
        return int(str(value).replace(",", "").replace("+", "").strip())
    except Exception:
        return None


def extract_growth_info(page_html):
    """从「社区成长」页解析香蕉余额、连续签到天数、签到奖励。"""
    text = strip_tags(page_html or "")
    info = {}

    m = re.search(r"连续签到[^\d]{0,6}([\d,]+)\s*天", text)
    if m:
        info["streak"] = to_int(m.group(1))

    for pat in (
        r"(?:香蕉余额|当前余额|我的余额|余额)[^\d]{0,8}([\d,]+)",
        r"香蕉[：:\s]*([\d,]+)",
    ):
        m = re.search(pat, text)
        if m:
            info["balance"] = to_int(m.group(1))
            break

    for pat in (
        r"(?:获得|奖励|领取)[^\d]{0,8}([\d,]+)\s*个?\s*香蕉",
        r"\+\s*([\d,]+)\s*个?\s*香蕉",
        r"每日签到[^\d+\-]{0,8}([+\-]?[\d,]+)",
        r"签到[^\d+\-]{0,10}([+\-][\d,]+)",
    ):
        m = re.search(pat, text)
        if m:
            info["reward"] = to_int(m.group(1))
            break

    if not info:
        idx = text.find("签到")
        if idx >= 0:
            log("页面签到相关文本: {}".format(text[max(0, idx - 60): idx + 160]))
    return info


def build_detail(info, prev=None, msg="", reward_prefix="本次获得", with_reward=True):
    """组装日志/通知中的签到详情。"""
    parts = []
    reward = info.get("reward")
    if prev and prev.get("balance") is not None and info.get("balance") is not None:
        diff = info["balance"] - prev["balance"]
        if diff > 0:
            reward = diff
    if with_reward and reward is not None and "获得" not in msg and "香蕉" not in msg:
        parts.append("{} +{} 香蕉".format(reward_prefix, reward))
    if info.get("streak") is not None:
        parts.append("连续签到 {} 天".format(info["streak"]))
    return "，".join(parts)


def do_checkin(sess, csrf):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": DOMAIN,
        "Referer": DOMAIN + "/growth",
    }
    return sess.post(
        DOMAIN + "/growth/checkin",
        data=json.dumps({}),
        headers=headers,
        timeout=20,
        allow_redirects=False,
    )


def parse_result(resp, sess):
    """返回 (ok, msg, page_html)，page_html 用于补充签到详情。"""
    text = resp.text or ""

    if resp.status_code == 401:
        return False, "Cookie 无效或已过期，请重新登录后更新 A2X_COOKIE", None
    if resp.status_code == 403:
        return False, "CSRF 校验失败（Cookie 不完整），请重新复制完整 Cookie", None
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "")
        if "login" in location.lower():
            return False, "Cookie 已失效，请重新登录后更新 A2X_COOKIE", None
        # 服务端跳回 /growth 属于正常的表单提交流程，跟随并读取页面提示
        try:
            followed = sess.get(urljoin(DOMAIN + "/", location or "/growth"), timeout=20)
        except Exception as e:
            return True, "签到请求已提交（跳转 {}），读取结果失败: {}".format(location, e), None
        if "login" in followed.url.lower():
            return False, "Cookie 已失效，请重新登录后更新 A2X_COOKIE", None
        ok, msg = judge_growth_page(followed.text)
        if msg:
            return ok, msg, followed.text
        return True, "签到请求已提交（跳转 {}），页面未发现明确提示，请核对香蕉余额".format(location), followed.text
    if is_already_checked(text):
        return True, "今日已签到（跳过）", text if "text/html" in (resp.headers.get("content-type") or "").lower() else None

    if "text/html" in (resp.headers.get("content-type") or "").lower():
        ok, msg = judge_growth_page(text)
        if msg:
            return ok, msg, text

    data = None
    try:
        data = resp.json()
    except Exception:
        pass

    if isinstance(data, dict):
        msg = data.get("message") or data.get("msg") or data.get("error")
        if resp.status_code >= 400 or data.get("error"):
            return False, str(msg or json.dumps(data, ensure_ascii=False)), None
        keys = ("reward", "amount", "bananas", "balance", "points", "days", "streak")
        brief = {k: data[k] for k in keys if k in data}
        if brief:
            return True, json.dumps(brief, ensure_ascii=False), None
        return True, str(msg or "签到成功"), None

    if resp.status_code >= 400:
        return False, "HTTP {}: {}".format(resp.status_code, text[:200]), None
    return True, "签到成功（HTTP {}）".format(resp.status_code), None


def pick_joke():
    return random.choice(JOKES)


def fetch_hitokoto():
    """调用每日一言接口，返回带出处的完整句子；失败返回 None。"""
    try:
        resp = requests.get(
            HITOKOTO_API,
            params={
                "encode": "json",
                "charset": "utf-8",
                "min_length": 10,
                "max_length": 100,
                "_": random.random(),  # 避免 CDN 缓存导致连续返回同一句
            },
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            },
            timeout=8,
        )
        data = resp.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    text = str(data.get("hitokoto") or "").strip()
    if not text:
        return None
    author = str(data.get("from_who") or "").strip()
    source = str(data.get("from") or "").strip()
    tail = " ".join(x for x in (author, source) if x)
    return "{} ——{}".format(text, tail) if tail else text


def normalize_reply_text(text):
    return re.sub(r"\s+", "", text or "")


def is_short_reply(text):
    """站点要求回帖至少 10 个文字或数字。"""
    return len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", text or "")) < 10


def pick_reply(used, today_texts=()):
    existing = [normalize_reply_text(t) for t in today_texts if t]

    def blocked(text):
        if not text or text in used or is_short_reply(text):
            return True
        norm = normalize_reply_text(text)
        for old in existing:
            if norm == old or norm.startswith(old) or old.startswith(norm):
                return True
        return False

    if REPLY_SOURCE == "hitokoto":
        for _ in range(3):
            text = fetch_hitokoto()
            if not blocked(text):
                return text
        log("每日一言过短或与今日内容重复，本次改用内置笑话")
    for _ in range(8):
        joke = pick_joke()
        if not blocked(joke):
            return joke
    return pick_joke()


def do_reply(sess, topic_id, body, csrf):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": DOMAIN,
        "Referer": "{}/t/{}".format(DOMAIN, topic_id),
    }
    return sess.post(
        "{}/t/{}/replies".format(DOMAIN, topic_id),
        data=json.dumps({"body": body}),
        headers=headers,
        timeout=20,
        allow_redirects=False,
    )


def classify_reply(resp):
    """返回 (kind, msg)，kind 为 ok / duplicate / limit / auth / error。"""
    text = resp.text or ""

    if resp.status_code in (401, 403):
        return "auth", "Cookie 失效或无回复权限（HTTP {}）".format(resp.status_code)
    if resp.status_code == 429:
        return "rate", "请求过于频繁（HTTP 429）"
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "")
        if "login" in location.lower():
            return "auth", "Cookie 已失效"
        return "ok", "已提交"

    if "text/html" in (resp.headers.get("content-type") or "").lower():
        flashes = extract_flash(text)
        if flashes:
            check = " ".join(flashes)
            if any(k in check for k in ("重复", "相同", "已存在")):
                return "duplicate", check
            if any(k in check for k in ("频繁", "太快", "稍后", "冷却", "rate")):
                return "rate", check
            if any(k in check for k in ("上限", "次数", "超出", "今日", "每天")):
                return "limit", check
            if any(k in check for k in ("登录", "权限", "禁言", "封禁")):
                return "auth", check
            if any(k in check for k in ("失败", "错误", "不能")):
                return "error", check
            return "ok", check
        return "ok", "已发布"

    data = None
    try:
        data = resp.json()
    except Exception:
        pass

    msg = ""
    if isinstance(data, dict):
        msg = str(data.get("message") or data.get("msg") or data.get("error") or "")

    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
        check = msg or text[:120]
        if any(k in check for k in ("重复", "相同", "已存在", "duplicate")):
            return "duplicate", check
        if any(k in check for k in ("频繁", "太快", "稍后", "冷却", "rate")):
            return "rate", check
        if any(k in check for k in ("上限", "次数", "超出", "今日", "每天")):
            return "limit", check
        if any(k in check for k in ("登录", "权限", "禁言", "封禁", "forbidden")):
            return "auth", check
        return "error", check

    return "ok", msg or "已发布"


def retry_after_seconds(resp, default):
    value = resp.headers.get("Retry-After")
    if value:
        try:
            return max(1, int(float(value)))
        except Exception:
            pass
    return int(default)


def get_today_replies(sess, uid):
    """读取个人主页回复记录，返回 (今日回复数, 今日回复内容片段)；失败返回 (None, [])。"""
    tz = datetime.timezone(datetime.timedelta(hours=8))  # 新加坡时间与 UTC+8 一致
    today = datetime.datetime.now(datetime.timezone.utc).astimezone(tz).date()
    count = 0
    texts = []
    for page in range(1, 4):
        url = "{}/u/{}?tab=replies".format(DOMAIN, uid)
        if page > 1:
            url += "&page={}".format(page)
        try:
            resp = None
            for attempt in range(3):
                try:
                    resp = sess.get(url, timeout=20)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise e
                    time.sleep(2)
        except Exception as e:
            log("读取回复记录失败: {}".format(e))
            return None, []
        if resp.status_code != 200:
            return (count, texts) if page > 1 else (None, [])

        items = re.findall(r'<article class="space-reply">(.*?)</article>', resp.text, re.S)
        if not items:
            break

        reached_old = False
        for item in items:
            m = re.search(r'<time datetime="([^"]+)"', item)
            if not m:
                continue
            try:
                dt = datetime.datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
            except Exception:
                continue
            if dt.astimezone(tz).date() != today:
                reached_old = True
                continue

            count += 1
            excerpt = re.search(r'<p class="space-reply-excerpt">(.*?)</p>', item, re.S)
            if excerpt:
                text = html_lib.unescape(re.sub(r"<[^>]+>", "", excerpt.group(1))).strip()
                if text:
                    texts.append(text)
        if reached_old:
            break
    return count, texts


def fetch_growth_html(sess):
    """读取成长页；若流水不在该页，再尝试常见的 tab 链接。"""
    fallback = ""
    for path in ("/growth", "/growth?tab=ledger", "/growth?tab=banana", "/growth?tab=flow"):
        try:
            resp = sess.get(DOMAIN + path, timeout=20)
        except Exception:
            continue
        if resp.status_code != 200:
            continue
        if not fallback:
            fallback = resp.text
        if "有效回帖" in resp.text or "香蕉流水" in resp.text:
            return resp.text
    return fallback


DATE_RE = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")
LEDGER_ROW_RE = re.compile(r"(回帖|签到)\s*[·•・]\s*#(\d+)")


def parse_ledger_entries(html):
    """解析香蕉流水里的回帖/签到记录，返回 [{kind, post, dt, amount, outcome}]。"""
    if not html:
        return []
    text = html_lib.unescape(re.sub(r"<[^>]+>", "\n", html))
    markers = list(LEDGER_ROW_RE.finditer(text))
    tz = datetime.timezone(datetime.timedelta(hours=8))
    entries = []
    for i, m in enumerate(markers):
        prev_end = markers[i - 1].end() if i else 0
        next_start = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        before = text[prev_end:m.start()]
        after = text[m.end():next_start]

        dt = None
        dates = list(DATE_RE.finditer(before))
        if dates:
            g = dates[-1].groups()
            try:
                dt = datetime.datetime(
                    int(g[0]), int(g[1]), int(g[2]),
                    int(g[3]), int(g[4]), int(g[5] or 0), tzinfo=tz,
                )
            except ValueError:
                dt = None

        amount = None
        amt = re.match(r"\s*([+-]?\d+)", after)
        if amt:
            try:
                amount = int(amt.group(1))
            except ValueError:
                amount = None

        if "有效回帖" in after:
            outcome = "有效回帖"
        elif "已达当日奖励上限" in after:
            outcome = "已达当日奖励上限"
        elif "每日签到" in after:
            outcome = "每日签到"
        else:
            outcome = ""
        entries.append({
            "kind": m.group(1),
            "post": m.group(2),
            "dt": dt,
            "amount": amount,
            "outcome": outcome,
        })
    return entries


def ledger_today_stats(entries):
    """返回 (今日有效回帖数, 今日是否已触发上限)；无法识别日期时返回 (None, False)。"""
    if not entries:
        return None, False
    tz = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(datetime.timezone.utc).astimezone(tz).date()
    valid = 0
    cap = False
    dated = 0
    for e in entries:
        if not e["dt"]:
            continue
        dated += 1
        if e["kind"] != "回帖":
            continue
        if e["dt"].date() != today:
            continue
        if e["outcome"] == "有效回帖":
            valid += 1
        elif e["outcome"] == "已达当日奖励上限":
            cap = True
    return (valid, cap) if dated else (None, False)


def ledger_today_checkin(entries):
    """返回 (今日是否已签到, 签到获得香蕉数)；流水不可用时返回 (None, None)。"""
    if not entries:
        return None, None
    tz = datetime.timezone(datetime.timedelta(hours=8))
    today = datetime.datetime.now(datetime.timezone.utc).astimezone(tz).date()
    for e in entries:
        if e["kind"] != "签到":
            continue
        if e["dt"] and e["dt"].date() == today:
            return True, e["amount"]
    return False, None


def ledger_new_entry(before, after):
    """返回比对基准之后新增的流水，没有则返回 None。"""
    old = {(e["kind"], e["post"], e["outcome"]) for e in before}
    for e in after:
        if (e["kind"], e["post"], e["outcome"]) not in old:
            return e
    return None


def wait_ledger_entry(sess, before, timeout=15.0):
    """回复后轮询成长页，等待香蕉流水出现新的回帖记录。"""
    deadline = time.time() + timeout
    latest = before
    while time.time() < deadline:
        time.sleep(3)
        latest = parse_ledger_entries(fetch_growth_html(sess))
        entry = ledger_new_entry(before, latest)
        if entry:
            return entry, latest
    return None, latest


def reply_for_account(sess):
    """在官方水楼回复每日一言/笑话，自动补足当天至多 REPLY_COUNT 条。"""
    remaining = REPLY_COUNT
    if remaining <= 0:
        return 0, "未配置回复条数"

    topic_id = REPLY_TOPICS[0]
    try:
        page = sess.get("{}/t/{}".format(DOMAIN, topic_id), timeout=20)
    except Exception as e:
        return 0, "打开帖子失败: {}".format(e)
    if "login" in page.url.lower():
        return 0, "Cookie 已失效，无法回复"
    csrf = csrf_from_html(page.text)
    if not csrf:
        return 0, "未获取到回复所需 CSRF Token"

    uid_match = re.search(r'data-dm-user="([A-Za-z0-9_-]+)"', page.text)
    if not uid_match:
        uid_match = re.search(r'<header[^>]*>.*?href="/u/([A-Za-z0-9_-]+)"', page.text, re.S)

    today_texts = []
    profile_count = None
    if uid_match:
        profile_count, today_texts = get_today_replies(sess, uid_match.group(1))

    ledger_entries = []
    ledger_count = None
    ledger_cap = False
    if VERIFY_LEDGER:
        ledger_entries = parse_ledger_entries(fetch_growth_html(sess))
        ledger_count, ledger_cap = ledger_today_stats(ledger_entries)

    if ledger_cap:
        return 0, "香蕉流水显示今日已达奖励上限，无需再回复"
    if ledger_count is not None:
        today_count, source = ledger_count, "香蕉流水有效回帖"
    elif profile_count is not None:
        today_count, source = profile_count, "个人主页回复"
    else:
        return 0, "未能读取今日回复记录，为避免超发本次跳过回复"

    remaining = max(0, REPLY_COUNT - today_count)
    log("{}显示今日 {} 条，本次计划回复 {} 条".format(source, today_count, remaining))
    if remaining == 0:
        label = "有效回帖" if ledger_count is not None else "回复"
        return 0, "今日{}已达 {} 条，无需再回复".format(label, today_count)

    ledger_gate = VERIFY_LEDGER and bool(ledger_entries)
    if ledger_gate:
        log("已开启香蕉流水校验：每条回复后确认流水是否新增")
    elif VERIFY_LEDGER:
        log("未识别到香蕉流水记录，跳过逐条校验")

    done = 0
    gained = 0
    note = ""
    used = set()
    delay = REPLY_DELAY
    attempts = 0
    while done < remaining and attempts < remaining * 3:
        attempts += 1
        body = pick_reply(used, today_texts)
        current_topic = REPLY_TOPICS[done % len(REPLY_TOPICS)]

        kind, msg = "error", ""
        retries = 0
        while True:
            try:
                resp = do_reply(sess, current_topic, body, csrf)
            except Exception as e:
                kind, msg = "error", "回复异常: {}".format(e)
                break

            kind, msg = classify_reply(resp)
            if kind == "rate" and retries < 3:
                wait = min(retry_after_seconds(resp, 15 * (2 ** retries)), 120)
                log("触发站点频率限制，{} 秒后重试（{}/3）".format(wait, retries + 1))
                time.sleep(wait)
                retries += 1
                delay = max(delay, float(wait))
                continue
            break

        if kind == "ok":
            done += 1
            gained += 1
            used.add(body)
            log("回复 {}/{}（t{}）: {}".format(done, remaining, current_topic, body))
            if ledger_gate:
                entry, ledger_entries = wait_ledger_entry(sess, ledger_entries)
                if entry is None:
                    gained -= 1
                    note = "回复已提交，但香蕉流水未见新记录，已停止后续回复（可能待审核或站点延迟）"
                    break
                if entry["outcome"] == "有效回帖" or (
                    not entry["outcome"] and (entry.get("amount") or 0) > 0
                ):
                    log("香蕉流水已记录『有效回帖 +1』")
                elif entry["outcome"] == "已达当日奖励上限":
                    gained -= 1
                    note = "香蕉流水显示已达当日奖励上限，已停止后续回复"
                    break
                else:
                    gained -= 1
                    note = "本条回复未获得香蕉奖励（可能待审核、内容重复或板块无奖励），已停止后续回复"
                    break
            if done < remaining:
                time.sleep(delay * random.uniform(0.8, 1.3))
        elif kind == "duplicate":
            used.add(body)
            note = "内容重复，已换下一条"
        elif kind == "rate":
            note = "站点频率限制，已停止剩余回复（可调大 A2X_REPLY_DELAY）"
            break
        elif kind == "limit":
            note = msg or "达到回复上限"
            break
        else:
            note = msg or "回复失败"
            break

    if done:
        if ledger_gate:
            text = "回复 {} 条（确认 +{} 香蕉）".format(done, gained)
        else:
            text = "回复 {} 条（预计 +{} 香蕉）".format(done, gained)
        return done, text + ("；{}".format(note) if note else "")
    return 0, "未新增回复：{}".format(note or "未知原因")


_NOTIFY_WARNED = False
_NOTIFY_SEND = None


def load_notify():
    """加载青龙面板内置的 notify.py，返回 send 函数；找不到返回 None。"""
    try:
        from notify import send  # 青龙内置通知模块
        return send
    except Exception:
        pass

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "notify.py"),
        "/ql/scripts/notify.py",
        "/ql/data/scripts/notify.py",
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            folder = os.path.dirname(path)
            if folder not in sys.path:
                sys.path.insert(0, folder)
            spec = importlib.util.spec_from_file_location("ql_notify", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, "send"):
                return module.send
        except Exception:
            continue
    return None


def notify(title, content):
    """调用青龙面板内置通知（notify.py）。"""
    global _NOTIFY_WARNED, _NOTIFY_SEND
    if _NOTIFY_SEND is None:
        _NOTIFY_SEND = load_notify() or False
    if _NOTIFY_SEND is False:
        if not _NOTIFY_WARNED:
            log("未找到青龙内置 notify 模块（notify.py），已跳过通知")
            _NOTIFY_WARNED = True
        return False
    try:
        _NOTIFY_SEND(title, content)
        log("通知已发送: {}".format(title))
        return True
    except Exception as e:
        log("通知发送失败: {}".format(e))
        return False


def checkin_for_account(sess):
    """执行签到，返回 (ok, msg)。优先用香蕉流水判断状态与奖励。"""
    # 先读取成长页：用于判断今日是否已签到，并作为余额对比基准
    pre = sess.get(DOMAIN + "/growth", timeout=20)
    if "login" in pre.url.lower():
        return False, "Cookie 已失效，请重新登录后更新 A2X_COOKIE"
    pre_html = pre.text
    pre_info = extract_growth_info(pre_html)

    signed, reward = ledger_today_checkin(parse_ledger_entries(pre_html))
    if not signed and page_says_signed(pre_html):
        signed, reward = True, pre_info.get("reward")

    if signed:
        msg = "今日已签到（本次未重复提交）"
        if reward is not None:
            msg += "，流水 +{} 香蕉".format(reward)
        detail = build_detail(pre_info, msg=msg, with_reward=False)
        return True, (msg + "，" + detail if detail else msg)

    csrf = csrf_from_html(pre_html) or get_csrf_token(sess)
    resp = do_checkin(sess, csrf)
    ok, msg, page_html = parse_result(resp, sess)

    if ok:
        # 签到后优先从香蕉流水读取实际到账数量
        html = page_html or ""
        for attempt in range(3):
            signed_now, got = ledger_today_checkin(parse_ledger_entries(html))
            if signed_now:
                if got is not None:
                    msg = "签到成功，流水 +{} 香蕉".format(got)
                else:
                    msg = "签到成功（流水已记录）"
                post_info = extract_growth_info(html) if html else {}
                detail = build_detail(post_info, pre_info, msg)
                if detail:
                    msg += "，" + detail
                return ok, msg
            if attempt < 2:
                time.sleep(2)
                html = fetch_growth_html(sess) or html

    if page_html:
        post_info = extract_growth_info(page_html)
        detail = build_detail(post_info, pre_info, msg)
        if detail:
            msg += "，" + detail
    return ok, msg


def current_balance(sess):
    """读取成长页当前香蕉余额，失败返回 None。"""
    try:
        info = extract_growth_info(fetch_growth_html(sess) or "")
        return info.get("balance")
    except Exception:
        return None


def clean_name(value):
    """从标题中取出用户名，例如「没什么事 · 1A2X社区」→「没什么事」。"""
    value = html_lib.unescape(value or "").strip()
    for sep in (" · ", " | ", " - ", " – "):
        if sep in value:
            value = value.split(sep, 1)[0].strip()
    value = re.sub(r"\s+", " ", value)
    return value[:40] or None


def profile_name(page_html):
    """从个人主页解析昵称：优先 og:title，其次 <title>。"""
    if not page_html:
        return None
    for pat in (
        r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
        r'<meta[^>]+content="([^"]+)"[^>]+property="og:title"',
        r"<title>([^<]+)</title>",
    ):
        m = re.search(pat, page_html, re.I)
        if m:
            name = clean_name(m.group(1))
            if name:
                return name
    return None


def get_account_name(sess):
    """读取当前账号昵称，失败返回 None。"""
    page_html = fetch_growth_html(sess) or ""
    uid_match = re.search(r'data-dm-user="([A-Za-z0-9_-]+)"', page_html)
    if uid_match:
        try:
            prof = sess.get("{}/u/{}".format(DOMAIN, uid_match.group(1)), timeout=15)
            if prof.status_code == 200 and "login" not in prof.url.lower():
                name = profile_name(prof.text)
                if name:
                    return name
        except Exception:
            pass
    m = re.search(r'data-dm-username="([^"]+)"', page_html)
    if m:
        name = clean_name(m.group(1))
        if name:
            return name
    return None


def run_account(index, cookie):
    log("===== 账号 {} 开始 =====".format(index))
    sess = new_session()
    name = None
    try:
        load_cookies(sess, cookie)

        name = get_account_name(sess)
        if name:
            log("账号昵称: {}".format(name))

        ok, msg = checkin_for_account(sess)
        log("签到结果: {} {}".format("成功" if ok else "失败", msg))
        checkin_part = msg if ok else "失败，{}".format(msg)

        reply_part = "未开启"
        if ok and REPLY_ENABLED and REPLY_COUNT > 0:
            _, reply_msg = reply_for_account(sess)
            reply_part = reply_msg
            msg += "；" + reply_msg
            log("回复结果: {}".format(reply_msg))
        elif not ok:
            reply_part = "未执行（签到失败）"

        balance = current_balance(sess)
        balance_part = "读取失败"
        if balance is not None:
            balance_part = "当前 {} 香蕉".format(balance)
            msg += "；当前余额 {} 香蕉".format(balance)
            log("当前余额: {} 香蕉".format(balance))

        log("结果: {} {}".format("成功" if ok else "失败", msg))
        parts = {"checkin": checkin_part, "reply": reply_part, "balance": balance_part}
        return ok, msg, name, parts
    except Exception as e:
        log("异常: {}".format(e))
        parts = {"checkin": "异常，{}".format(e), "reply": "未执行", "balance": "未读取"}
        return False, str(e), name, parts
    finally:
        sess.close()


def main():
    cookies = [c for c in re.split(r"[\r\n&]+", COOKIE_ENV) if c.strip()]
    if not cookies:
        msg = (
            "未配置环境变量 A2X_COOKIE，请填入登录后的 Cookie。\n"
            "获取方式：浏览器登录 1a2x.com → F12 → Network → 任意请求 → 复制完整 Cookie"
        )
        log(msg)
        notify("1A2X 签到未执行", msg)
        sys.exit(1)

    log("共 {} 个账号，站点 {}".format(len(cookies), DOMAIN))
    if REPLY_ENABLED and REPLY_COUNT > 0:
        log(
            "自动回复已开启：帖子 {}，本次最多 {} 条，间隔约 {} 秒，内容来源 {}".format(
                REPLY_TOPICS, REPLY_COUNT, int(REPLY_DELAY), "每日一言" if REPLY_SOURCE == "hitokoto" else "内置笑话"
            )
        )
    else:
        log("自动回复未开启")
    results = []
    for i, cookie in enumerate(cookies, 1):
        results.append(run_account(i, cookie))
        if i < len(cookies):
            time.sleep(1)

    success = sum(1 for ok, _, _, _ in results if ok)
    if success == len(results):
        title = "1A2X 签到成功 {}/{}".format(success, len(results))
    else:
        title = "1A2X 签到失败 {}/{}".format(len(results) - success, len(results))
    blocks = []
    for i, (_, _, name, parts) in enumerate(results, 1):
        label = "账号{}{}：".format(i, "（{}）".format(name) if name else "")
        blocks.append(
            "\n".join([
                label,
                "签到：{}；".format(parts["checkin"]),
                "回复：{}；".format(parts["reply"]),
                "余额：{}。".format(parts["balance"]),
            ])
        )
    content = "\n\n".join(blocks)
    log("===== 汇总 =====")
    log(content)
    notify(title, content)
    sys.exit(0 if success == len(results) else 1)


def entry():
    """捕获未处理异常并发送异常通知。"""
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        err = traceback.format_exc()
        log("脚本运行异常:\n{}".format(err))
        notify("1A2X 签到脚本运行异常", err[-1500:])
        sys.exit(1)


if __name__ == "__main__":
    entry()
