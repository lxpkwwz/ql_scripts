# -*- coding: utf-8 -*-
"""
cron: 5 0 * * *
new Env('FurryBox每日签到')
FurryBox 每日签到 — 青龙面板 Python 脚本（含通知）

站点: https://my.furry.box/wallet
规则(2026-11-27 实测页面 + bundle.js 逆向确认):
  - 每天北京时间 0 点后可签到 1 次，每次固定 +5 boxes
  - 注册域名 10 boxes / 续期 5 boxes
  - 今日已签到时再调签到接口会返回 ALREADY_SIGNED_IN，本脚本会先查流水幂等跳过
  - 当前前端 bundle 里的 Turnstile sitekey 为空，即钱包页目前不挂人机验证，
    签到可不带 turnstile_token；若服务端日后开启验证并返回 TURNSTILE_FAILED，
    脚本无法代过验证，需当天手动在浏览器里点一次签到

接口(从 https://my.furry.box/bundle.js 确认):
  Supabase: https://pwfcgdrorwpdzatxavdt.supabase.co
  Edge Function: POST /functions/v1/auth
    {"action":"wallet"}       -> {"wallet": {"balance": N}}
    {"action":"transactions"} -> {"transactions": [{"created_at","reason","amount","balance_after"}]}
    {"action":"signin"}       -> 成功 {"amount":5,"balance":N}
                                已签到 {"error":"ALREADY_SIGNED_IN",...}
  刷新 token: POST /auth/v1/token?grant_type=refresh_token
    Header apikey: sb_publishable_KWmBOppH3DRoJf4-g9UCwQ_qLUZojRR (公开的 publishable key，写在前端 JS 里)
    Body {"refresh_token": "..."} -> {"access_token": "...", "refresh_token": "...", "user": {...}}

如何拿到 token(只做一次):
  在已登录 FurryBox 的浏览器里打开 https://my.furry.box/wallet，
  按 F12 -> Application/应用 -> Cookies -> .furry.box，复制 fbx_rt 的值；
  或 Application -> Local Storage -> fbx-session，里面 JSON 的 refresh_token。
  access_token (fbx_at) 只有 1 小时有效，别只填它；refresh_token 有效期 30 天，
  脚本每次用它换新的 access_token 来签到。

青龙环境变量(在「环境变量」里新建):
  FURRY_REFRESH_TOKEN   必填，refresh_token；多账号用 & 或换行分隔
  FURRY_ACCESS_TOKEN    选填，access_token；只有它时也能跑 1 小时内的一次，不推荐
  FURRY_TURNSTILE_TOKEN 选填，万一日后开启验证时再填(正常留空)
  通知: 脚本会先向上找青龙脚本根目录的 notify.py(脚本目录 -> 父目录 ->
    /ql/data/scripts)，找到就用它发；找不到才按青龙标准环境变量名内置直发:
    PUSH_PLUS_TOKEN / BARK_PUSH / SCKEY 或 SERVERCHAN_SENDKEY /
    TG_BOT_TOKEN+TG_USER_ID / DD_BOT_TOKEN / QYWX_KEY 等。
    配套的 notify.py 只是备用，不要用它覆盖根目录已有的 notify.py。

注意:
  - Supabase 刷新时可能轮换 refresh_token；若日志里出现「refresh_token 已轮换，
    请更新环境变量」，请把新值贴回 FURRY_REFRESH_TOKEN，否则 30 天后会失效。
  - 今天(2026-11-27)你已签到 +5，余额 5；首次跑脚本应显示「今日已签到」，这是正常的幂等结果。
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    print("缺少 requests 库，请在青龙依赖管理里安装 requests 后再跑")
    sys.exit(1)

# ---------------- 配置 ----------------
SUPABASE_URL = "https://pwfcgdrorwpdzatxavdt.supabase.co"
PUBLISHABLE_KEY = "sb_publishable_KWmBOppH3DRoJf4-g9UCwQ_qLUZojRR"
AUTH_FN_URL = f"{SUPABASE_URL}/functions/v1/auth"
TOKEN_URL = f"{SUPABASE_URL}/auth/v1/token?grant_type=refresh_token"
CST = timezone(timedelta(hours=8))  # 北京时间，签到按北京时间 0 点重置

# ---------------- 通知 ----------------
# 青龙脚本根目录自带 notify.py 和 sendNotify.js，但任务运行时脚本
# 所在目录不一定就是根目录(例如脚本放在子文件夹里)，直接
# `from notify import send` 会报 No module named 'notify'。
# 所以这里先向上逐级找 notify.py(脚本目录 -> 父目录 -> /ql/data/scripts)，
# 找到就把它所在目录加进 sys.path 再 import；找不到才走内置直发。
# 注意: 不要用配套的 notify.py 覆盖根目录已有的 notify.py，
# 根目录那个才是跟你的青龙通知配置配套的。
def _find_notify_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = ["/ql/data/scripts", "/ql/scripts"]
    d = os.path.dirname(here)  # 先从父目录往上找(根目录优先于脚本同目录)
    for _ in range(4):
        candidates.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    candidates += [here, os.getcwd()]
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        if os.path.isfile(os.path.join(c, "notify.py")):
            return c
    return None


def notify(title, content):
    """根目录 notify.py 优先，否则按环境变量直接走各推送渠道。"""
    ndir = _find_notify_dir()
    if ndir:
        if ndir not in sys.path:
            sys.path.insert(0, ndir)
        try:
            from notify import send as ql_send
            result = ql_send(title, content)
            if result is False:
                print("[通知] notify.py 返回失败，改走内置直发")
            else:
                return  # 无异常即视为已发送(青龙 notify.py 的 send 一般无返回值)
        except Exception as e:
            print(f"[通知] notify.py 不可用({ndir}): {e}，改走内置直发")
    else:
        print("[通知] 未找到 notify.py，改走内置直发")
    import urllib.parse
    sent = False
    try:
        if os.environ.get("PUSH_PLUS_TOKEN"):
            try:
                requests.post("http://www.pushplus.plus/send", json={
                    "token": os.environ["PUSH_PLUS_TOKEN"],
                    "title": title, "content": content.replace("\n", "<br>"),
                }, timeout=10)
                sent = True
            except Exception as e:
                print(f"[通知] PushPlus 失败: {e}")
        bark = (os.environ.get("BARK_PUSH") or os.environ.get("BARK_URL") or "").strip().rstrip("/")
        if bark:
            try:
                requests.get(bark + "/" + urllib.parse.quote(title) + "/"
                             + urllib.parse.quote(content), timeout=10)
                sent = True
            except Exception as e:
                print(f"[通知] Bark 失败: {e}")
        sc = (os.environ.get("SERVERCHAN_SENDKEY") or os.environ.get("SCKEY") or "").strip()
        if sc:
            try:
                requests.post(f"https://sctapi.ftqq.com/{sc}.send",
                              data={"title": title, "desp": content}, timeout=10)
                sent = True
            except Exception as e:
                print(f"[通知] Server酱失败: {e}")
        if os.environ.get("TG_BOT_TOKEN") and os.environ.get("TG_USER_ID"):
            try:
                host = os.environ.get("TG_API_HOST", "api.telegram.org")
                requests.post(
                    f"https://{host}/bot{os.environ['TG_BOT_TOKEN']}/sendMessage",
                    json={"chat_id": os.environ["TG_USER_ID"],
                          "text": f"{title}\n{content}"}, timeout=10)
                sent = True
            except Exception as e:
                print(f"[通知] Telegram 失败: {e}")
    except Exception as e:
        print(f"[通知] 推送渠道发送失败: {e}")
    if not sent:
        print("[通知] 未配置推送渠道，仅打印结果(青龙日志里可见)。"
              "请在青龙「环境变量」里配一个: PUSH_PLUS_TOKEN / BARK_PUSH / "
              "SCKEY / TG_BOT_TOKEN+TG_USER_ID 等")

# ---------------- 工具 ----------------
def split_tokens(raw):
    if not raw:
        return []
    parts = []
    for chunk in raw.replace("\n", "&").replace(";", "&").split("&"):
        chunk = chunk.strip()
        if not chunk:
            continue
        # 允许直接贴 Cookie 串或 fbx-session JSON
        if "fbx_rt=" in chunk:
            chunk = chunk.split("fbx_rt=")[1].split(";")[0].strip()
        elif chunk.startswith("{"):
            try:
                chunk = json.loads(chunk).get("refresh_token", chunk)
            except Exception:
                pass
        parts.append(chunk)
    return parts

def today_cst():
    return datetime.now(CST).strftime("%Y-%m-%d")

def api_auth(access_token, action, extra=None):
    """调 Edge Function /auth，401 时由调用方刷新重试。"""
    body = {"action": action}
    if extra:
        body.update(extra)
    r = requests.post(AUTH_FN_URL, json=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }, timeout=20)
    try:
        data = r.json()
    except Exception:
        data = {"_raw": r.text[:300], "_status": r.status_code}
    return r.status_code, data

def refresh_access_token(refresh_token):
    """用 refresh_token 换 access_token；返回 (access_token, new_refresh_token, err)。"""
    try:
        r = requests.post(TOKEN_URL, json={"refresh_token": refresh_token},
                          headers={"Content-Type": "application/json",
                                   "apikey": PUBLISHABLE_KEY}, timeout=20)
    except Exception as e:
        return None, None, f"刷新 token 网络失败: {e}"
    try:
        data = r.json()
    except Exception:
        return None, None, f"刷新 token 返回非 JSON(HTTP {r.status_code}): {r.text[:200]}"
    if r.status_code in (400, 401, 403) or not data.get("access_token"):
        return None, None, (
            f"refresh_token 无效或已过期(HTTP {r.status_code}): "
            f"{data.get('error_description') or data.get('msg') or data.get('message') or data}"
            "，请重新在浏览器 Cookie 里复制 fbx_rt 更新到环境变量")
    return data["access_token"], data.get("refresh_token"), None

def already_signed_today(access_token):
    """查流水：今天(北京时间)是否已有 reason=signin 的记录。"""
    status, data = api_auth(access_token, "transactions")
    if status != 200:
        return False, f"查流水失败(HTTP {status}): {data}"
    txs = data.get("transactions") or []
    today = today_cst()
    for tx in txs:
        created = str(tx.get("created_at") or "")[:10]
        # created_at 是 UTC 时间戳时也按北京时间换算一次
        try:
            dt = datetime.fromisoformat(
                str(tx.get("created_at")).replace("Z", "+00:00"))
            created = dt.astimezone(CST).strftime("%Y-%m-%d")
        except Exception:
            pass
        if tx.get("reason") == "signin" and created == today:
            return True, tx
    return False, None

def get_balance(access_token):
    status, data = api_auth(access_token, "wallet")
    if status != 200:
        return None, f"查余额失败(HTTP {status}): {data}"
    wallet = data.get("wallet") or data
    return wallet.get("balance"), None

# ---------------- 单账号 ----------------
def run_one(idx, refresh_token, access_token, turnstile_token):
    tag = f"账号{idx}"
    lines = []
    new_refresh = None

    if refresh_token:
        access_token, new_refresh, err = refresh_access_token(refresh_token)
        if err:
            return f"❌ {tag}: {err}"
        if new_refresh and new_refresh != refresh_token:
            lines.append(f"⚠️ {tag}: refresh_token 已轮换，请把新值更新到 FURRY_REFRESH_TOKEN"
                        f"(新值见本次日志末尾，勿外传)")
    if not access_token:
        return f"❌ {tag}: 没有可用的 access_token"

    # 幂等：先查今天是否已签到
    done, info = already_signed_today(access_token)
    if isinstance(info, str):  # 查流水失败也继续尝试签到，让服务端判重
        print(f"[{tag}] {info}，继续尝试签到")
    elif done:
        bal, _ = get_balance(access_token)
        return f"✅ {tag}: 今日已签到(+5 boxes)，当前余额 {bal} boxes"

    # 签到
    extra = {"turnstile_token": turnstile_token} if turnstile_token else {}
    status, data = api_auth(access_token, "signin", extra)
    if status == 401 and refresh_token:
        # access_token 刚过期，刷新一次再试
        access_token, new_refresh, err = refresh_access_token(refresh_token)
        if not err:
            status, data = api_auth(access_token, "signin", extra)
    if status == 200:
        amount = data.get("amount", 5)
        bal = data.get("balance")
        if bal is None:
            bal, _ = get_balance(access_token)
        lines.append(f"✅ {tag}: 签到成功 +{amount} boxes，当前余额 {bal} boxes")
    else:
        code = str(data.get("error") or data.get("code") or "")
        msg = data.get("message") or data.get("msg") or data
        if code == "ALREADY_SIGNED_IN" or "ALREADY_SIGNED_IN" in str(msg) or "已经签到" in str(msg):
            bal, _ = get_balance(access_token)
            lines.append(f"✅ {tag}: 今日已签到，当前余额 {bal} boxes")
        elif code == "TURNSTILE_FAILED" or "TURNSTILE_FAILED" in str(msg):
            lines.append(f"❌ {tag}: 人机验证未通过(TURNSTILE_FAILED)，"
                        "今天请手动在浏览器里点一次签到，脚本无法代过验证")
        elif code == "ONBOARDING_REQUIRED":
            lines.append(f"❌ {tag}: 账号未完成注册(需填邀请码)，请先在网页完成 onboarding")
        else:
            lines.append(f"❌ {tag}: 签到失败(HTTP {status}): {msg}")
    if new_refresh and new_refresh != refresh_token:
        lines.append(f"   新 refresh_token(请更新环境变量): {new_refresh}")
    return "\n".join(lines)

# ---------------- 主入口 ----------------
def main():
    refresh_tokens = split_tokens(os.environ.get("FURRY_REFRESH_TOKEN", ""))
    access_tokens = split_tokens(os.environ.get("FURRY_ACCESS_TOKEN", ""))
    turnstile_token = os.environ.get("FURRY_TURNSTILE_TOKEN", "").strip()
    accounts = max(len(refresh_tokens), len(access_tokens))
    if accounts == 0:
        msg = ("未配置 FURRY_REFRESH_TOKEN 或 FURRY_ACCESS_TOKEN。\n"
               "去浏览器 https://my.furry.box/wallet 按 F12 → Application → Cookies "
               "→ .furry.box，复制 fbx_rt 填到青龙环境变量 FURRY_REFRESH_TOKEN。")
        print(msg)
        notify("FurryBox签到失败", msg)
        return

    results = []
    for i in range(accounts):
        rt = refresh_tokens[i] if i < len(refresh_tokens) else ""
        at = access_tokens[i] if i < len(access_tokens) else ""
        try:
            results.append(run_one(i + 1, rt, at, turnstile_token))
        except Exception as e:
            results.append(f"❌ 账号{i + 1}: 运行异常: {e}")
        if i < accounts - 1:
            time.sleep(2)

    title = "FurryBox免费域名签到"
    content = f"{today_cst()} (北京时间)\n" + "\n".join(results)
    print(content)
    notify(title, content)

if __name__ == "__main__":
    main()
