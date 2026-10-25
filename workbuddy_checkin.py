#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 每日签到（青龙面板版，无第三方依赖）。

环境变量（只需一个）：
  WORKBUDDY：支持以下任一格式：
    1. 完整 desktop.info JSON（其中必须存在明文 refreshToken 和 account.uid）；
    2. refreshToken#UID#备注；
    3. {"refreshToken":"...","uid":"...","endpoint":"https://www.workbuddy.cn"}。
  WORKBUDDY_ENDPOINT：可选覆盖地址；不设置时默认 https://copilot.tencent.com。

重要：新版 workbuddy-desktop.info 如果只剩
{"$wbEncrypted":1,"envelope":"..."}，文件本身不能直接还原 refreshToken。
需要从仍登录的 WorkBuddy 客户端获取明文 refreshToken，然后按上述格式放入 WORKBUDDY。
脚本不会尝试或输出解密密钥。
"""

import base64
import datetime
import json
import logging
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENV_INFO = "WORKBUDDY"
ENV_REFRESH = "WORKBUDDY_REFRESH_TOKEN"
ENV_REFRESH_FILE = "WORKBUDDY_REFRESH_TOKEN_FILE"
ENV_TOKEN = "WORKBUDDY_ACCESS_TOKEN"
ENV_UID = "WORKBUDDY_UID"
BASE_URL = (os.environ.get("WORKBUDDY_ENDPOINT") or "https://copilot.tencent.com").rstrip("/")
DEFAULT_REFRESH_FILE = "/ql/data/config/workbuddy_refresh_token.txt"
ENDPOINT_REFRESH = BASE_URL + "/v2/plugin/auth/token/refresh"
ENDPOINT_STATUS = BASE_URL + "/v2/billing/meter/checkin-activity-status"
ENDPOINT_CHECKIN = BASE_URL + "/v2/billing/meter/daily-checkin"
ENDPOINT_RESOURCE = BASE_URL + "/v2/billing/meter/get-user-resource"
RESOURCE_BODY = {"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3], "OnlyValidPeriod": True}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("wb-checkin")


def token_file_candidates():
    e = os.environ
    return [p for p in (
        os.path.join(e.get("LOCALAPPDATA", ""), "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"),
        os.path.join(e.get("APPDATA", ""), "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"),
        os.path.expanduser("~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"),
        os.path.expanduser("~/.config/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"),
    ) if p]


def parse_line(line):
    parts = (line or "").strip().split("#")
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return {"refresh": parts[0].strip(), "uid": parts[1].strip(), "remark": "#".join(parts[2:]).strip() or "(未备注)", "src": "<env:WORKBUDDY refreshToken>"}


def find_plain_string(obj, names):
    """递归查找指定字段的明文字符串；跳过 $wbEncrypted 信封，避免误取 envelope。"""
    if isinstance(obj, dict):
        if obj.get("$wbEncrypted") or "envelope" in obj:
            return ""
        for name in names:
            value = obj.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = find_plain_string(value, names)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_plain_string(value, names)
            if found:
                return found
    return ""


def parse_info(raw):
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError) as e:
        log.warning("WORKBUDDY JSON 无法解析：%s", e)
        return None
    if isinstance(obj, list):
        if len(obj) == 1 and isinstance(obj[0], dict):
            obj = obj[0]
        else:
            log.warning("WORKBUDDY JSON 根节点是数组；请粘贴完整 workbuddy-desktop.info 文件内容")
            return None
    if not isinstance(obj, dict):
        return None
    auth = obj.get("auth") or {}
    account = obj.get("account") or {}
    if not isinstance(auth, dict):
        auth = {}
    if not isinstance(account, dict):
        account = {}
    uid = str(account.get("uid") or find_plain_string(obj, ("uid", "userId", "user_id")) or os.environ.get(ENV_UID) or "").strip()
    remark = str(account.get("nickname") or account.get("uin") or "(desktop.info)").strip()
    # WorkBuddy 5.6.0+ 常见结构：auth.* 是加密对象，但顶层仍保留可用的明文 JWT。
    # 必须先读顶层字段，不能把 auth.refreshToken 的 envelope 当令牌。
    top_refresh = obj.get("refreshToken")
    top_access = obj.get("accessToken")
    refresh = str(top_refresh or "").strip() if isinstance(top_refresh, str) else ""
    access = str(top_access or "").strip() if isinstance(top_access, str) else ""
    if not refresh:
        refresh = find_plain_string(obj, ("refreshToken", "refresh_token"))
    if not access:
        access = find_plain_string(obj, ("accessToken", "access_token"))
    if not refresh:
        auth_refresh = auth.get("refreshToken")
        refresh = str(auth_refresh or "").strip() if isinstance(auth_refresh, str) else ""
    if not access:
        auth_access = auth.get("accessToken")
        access = str(auth_access or "").strip() if isinstance(auth_access, str) else ""
    if refresh:
        domain = str(auth.get("domain") or obj.get("domain") or obj.get("endpoint") or "").strip()
        if domain.startswith("https://") or domain.startswith("http://"):
            endpoint = domain.rstrip("/")
        else:
            endpoint = ("https://" + domain.rstrip("/") if domain.startswith(("www.", "copilot.")) else "")
        return {"refresh": refresh, "uid": uid, "remark": remark, "src": "<env:WORKBUDDY refreshToken>", "endpoint": endpoint}
    if access and uid:
        domain = str(auth.get("domain") or obj.get("domain") or obj.get("endpoint") or "").strip()
        if domain.startswith("https://") or domain.startswith("http://"):
            endpoint = domain.rstrip("/")
        else:
            endpoint = ("https://" + domain.rstrip("/") if domain.startswith(("www.", "copilot.")) else "")
        return {"token": access, "uid": uid, "remark": remark, "src": "<env:WORKBUDDY accessToken>", "endpoint": endpoint}
    if obj.get("$wbEncrypted") or obj.get("envelope") or isinstance(auth.get("refreshToken"), dict) or isinstance(auth.get("accessToken"), dict):
        log.error("检测到新版加密字段，但未找到顶层明文 refreshToken/accessToken；请粘贴完整 desktop.info 文件，而不是 auth 内的 envelope")
        return {"encrypted": True, "uid": uid}
    return None


def load_accounts():
    raw = (os.environ.get(ENV_INFO) or "").strip()
    parsed = parse_info(raw) if raw.startswith("{") else None
    if parsed and not parsed.get("encrypted"):
        refresh_file = os.environ.get(ENV_REFRESH_FILE) or DEFAULT_REFRESH_FILE
        if parsed.get("refresh") and os.path.isfile(refresh_file):
            try:
                with open(refresh_file, encoding="utf-8") as f:
                    rotated = f.read().strip()
                if rotated:
                    parsed["refresh"] = rotated
                    parsed["src"] = refresh_file
            except OSError:
                pass
        return [parsed]

    refresh_file = os.environ.get(ENV_REFRESH_FILE) or DEFAULT_REFRESH_FILE
    refresh = ""
    if os.path.isfile(refresh_file):
        try:
            with open(refresh_file, encoding="utf-8") as f:
                refresh = f.read().strip()
        except OSError as e:
            log.warning("读取 refreshToken 文件失败 %s：%s", refresh_file, e)
    if not refresh:
        refresh = (os.environ.get(ENV_REFRESH) or "").strip()
    if refresh:
        uid = (os.environ.get(ENV_UID) or "").strip()
        if parsed and parsed.get("uid"):
            uid = parsed["uid"]
        return [{"refresh": refresh, "uid": uid, "remark": "(refreshToken)", "src": refresh_file}]

    accounts = []
    if raw and not raw.startswith("{"):
        for line in raw.splitlines():
            acc = parse_line(line)
            if acc:
                accounts.append(acc)
        if accounts:
            return accounts

    access = (os.environ.get(ENV_TOKEN) or "").strip()
    uid = (os.environ.get(ENV_UID) or "").strip()
    if access and uid:
        return [{"token": access, "uid": uid, "remark": "(env单账号)", "src": "<env:WORKBUDDY_ACCESS_TOKEN>"}]

    for path in token_file_candidates():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                acc = parse_info(f.read())
            if acc and not acc.get("encrypted"):
                acc["src"] = path
                return [acc]
        except Exception as e:
            log.warning("读取登录文件失败 %s：%s", path, e)
    return []


def decode_exp(token):
    try:
        part = token.split(".")[1] + "=" * (-len(token.split(".")[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8")).get("exp")
    except Exception:
        return None


def request_json(url, headers=None, payload=None, method="POST"):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except ValueError:
                return r.status, {"raw": raw[:500]}
    except HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:500]}
    except URLError as e:
        return 0, {"error": str(e)}


def dig(obj, *keys):
    if isinstance(obj, dict):
        for key in keys:
            if obj.get(key) is not None:
                return obj[key]
        for key in ("data", "result", "response", "resp"):
            if isinstance(obj.get(key), dict):
                value = dig(obj[key], *keys)
                if value is not None:
                    return value
    return None


def refresh_session(refresh_token):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Refresh-Token": refresh_token,
        "X-Auth-Refresh-Source": "plugin",
        "X-Domain": "copilot.tencent.com",
        "User-Agent": "WorkBuddy",
    }
    status, obj = request_json(ENDPOINT_REFRESH, headers, {})
    access = dig(obj, "accessToken", "access_token")
    new_refresh = dig(obj, "refreshToken", "refresh_token") or refresh_token
    uid = dig(obj, "uid", "userId", "user_id")
    if status < 200 or status >= 300 or not access:
        return None, None, uid, status, obj
    return str(access), str(new_refresh), uid, status, obj


def set_endpoint(endpoint):
    """按 desktop.info 的 domain 切换 CN 接口；环境变量 WORKBUDDY_ENDPOINT 优先。"""
    global BASE_URL, ENDPOINT_REFRESH, ENDPOINT_STATUS, ENDPOINT_CHECKIN, ENDPOINT_RESOURCE
    if os.environ.get("WORKBUDDY_ENDPOINT") or not endpoint:
        return
    BASE_URL = endpoint.rstrip("/")
    ENDPOINT_REFRESH = BASE_URL + "/v2/plugin/auth/token/refresh"
    ENDPOINT_STATUS = BASE_URL + "/v2/billing/meter/checkin-activity-status"
    ENDPOINT_CHECKIN = BASE_URL + "/v2/billing/meter/daily-checkin"
    ENDPOINT_RESOURCE = BASE_URL + "/v2/billing/meter/get-user-resource"


def save_refresh_token(token):
    """保存服务端轮换后的 refreshToken；文件权限尽量限制为仅当前用户可读。"""
    path = os.environ.get(ENV_REFRESH_FILE) or DEFAULT_REFRESH_FILE
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        log.info("已保存轮换后的 refreshToken：%s", path)
    except OSError as e:
        log.warning("保存轮换后的 refreshToken 失败 %s：%s", path, e)


def auth_headers(token, uid=""):
    h = {"Accept": "application/json", "Authorization": "Bearer " + token, "Content-Type": "application/json", "User-Agent": "WorkBuddy"}
    if uid:
        h["X-User-Id"] = uid
    return h


def checkin(token, uid):
    return request_json(ENDPOINT_CHECKIN, auth_headers(token, uid), {})


def query_balance(token, uid):
    status, obj = request_json(ENDPOINT_RESOURCE, auth_headers(token, uid), RESOURCE_BODY)
    if status != 200:
        return None
    try:
        items = (((obj.get("data") or {}).get("Response") or {}).get("Data") or {}).get("Accounts") or []
        total = 0.0
        for item in items:
            for key in ("CycleCapacityRemainPrecise", "CycleCapacityRemain", "CapacityRemainPrecise", "CapacityRemain"):
                if item.get(key) is not None:
                    total += float(item[key]); break
        return int(round(total))
    except Exception:
        return None


def result_text(status, obj):
    code = obj.get("code") if isinstance(obj, dict) else None
    msg = (obj.get("msg") or obj.get("message") or "") if isinstance(obj, dict) else ""
    if code == 10001 or "已签到" in msg or "already" in msg.lower():
        return True, "今日已签到(幂等命中)"
    if status in (401, 403):
        return False, "令牌失效(%s)，请更新 refreshToken 或重新登录 WorkBuddy" % status
    if 200 <= status < 300 and code in (None, 0):
        credit = dig(obj, "credit")
        return True, "签到成功" + (("，本次 +%s 积分" % credit) if credit is not None else "")
    return False, "签到失败 status=%s code=%s msg=%s" % (status, code, msg or obj.get("raw", "") if isinstance(obj, dict) else "")


def ql_notify(title, content):
    paths = [os.path.dirname(os.path.abspath(__file__)), "/ql/scripts", "/ql/data/scripts"]
    for p in paths:
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    try:
        import notify
        for name in ("send", "send_notify"):
            fn = getattr(notify, name, None)
            if callable(fn):
                fn(title, content); log.info("[通知] 已通过青龙系统通知 (%s.%s)", name, name); return True
    except Exception as e:
        log.warning("青龙 notify.py 调用失败：%s", e)
    try:
        import sendNotify
        fn = getattr(sendNotify, "sendNotify", None)
        if callable(fn):
            try: fn().send(title, content)
            except (AttributeError, TypeError): fn(title, content)
            log.info("[通知] 已通过青龙系统通知 (sendNotify)"); return True
    except Exception as e:
        log.warning("青龙 sendNotify.py 调用失败：%s", e)
    log.warning("未找到可用的青龙系统通知模块")
    return False


def run_account(acc):
    remark, uid = acc.get("remark", "(账号)"), acc.get("uid", "")
    set_endpoint(acc.get("endpoint"))
    token = acc.get("token")
    if acc.get("refresh"):
        token, new_refresh, new_uid, status, body = refresh_session(acc["refresh"])
        if new_uid and not uid: uid = str(new_uid)
        if not token:
            msg = "刷新令牌失败 status=%s，请重新获取 refreshToken；响应=%s" % (status, json.dumps(body, ensure_ascii=False)[:240])
            log.error("账号[%s] ❌ %s", remark, msg)
            return {"remark": remark, "ok": False, "msg": msg, "before": None, "after": None}
        if new_refresh:
            save_refresh_token(new_refresh)
        log.info("账号[%s] 已通过 refreshToken 获取新 accessToken", remark)
    else:
        new_refresh = None
    if not token:
        return {"remark": remark, "ok": False, "msg": "没有可用 accessToken", "before": None, "after": None}
    exp = decode_exp(token)
    if exp:
        log.info("账号[%s] accessToken 剩余约 %s", remark, datetime.datetime.fromtimestamp(exp) - datetime.datetime.now())
    before = query_balance(token, uid)
    if before is not None: log.info("账号[%s] 签到前总积分: %s", remark, before)
    status, body = checkin(token, uid)
    ok, msg = result_text(status, body)
    after = query_balance(token, uid)
    if after is not None: log.info("账号[%s] 签到后总积分: %s", remark, after)
    if ok: log.info("账号[%s] ✅ %s", remark, msg)
    else: log.error("账号[%s] ❌ %s | 响应: %s", remark, msg, json.dumps(body, ensure_ascii=False)[:300])
    return {"remark": remark, "ok": ok, "msg": msg, "before": before, "after": after, "gained": (after - before) if before is not None and after is not None else None}


def main():
    accounts = load_accounts()
    if not accounts:
        log.error("没有找到可用登录态。请在唯一环境变量 WORKBUDDY 中填写明文 refreshToken#UID，或填写包含明文 refreshToken 的 JSON")
        return 2
    if any(a.get("encrypted") for a in accounts):
        return 2
    log.info("共加载 %d 个账号，开始签到", len(accounts))
    results = [run_account(a) for a in accounts]
    failed = sum(1 for r in results if not r["ok"])
    lines = ["WorkBuddy 每日签到（共 %d 个账号）" % len(results), "=" * 28]
    for r in results:
        lines += ["【%s】" % r["remark"], "结果：%s" % (("✅ " if r["ok"] else "❌ ") + r["msg"])]
        if r.get("before") is not None or r.get("after") is not None:
            lines.append("总积分：%s → %s" % (r.get("before", "—"), r.get("after", "—")))
    content = "\n".join(lines)
    ql_notify("WorkBuddy 签到 %s成功 %s失败" % (len(results) - failed, failed), content)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
