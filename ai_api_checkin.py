import argparse
import json
import os
from pathlib import Path
import sys
import time
import requests

# ============================================================================
# 代理配置 —— 硬编码，绕过青龙 task 环境变量不传递的问题
# 如果不需要代理，把 PROXIES 改成 None 即可
# ============================================================================
PROXIES = {
    "http": "http://172.17.0.1:7890",
    "https": "http://172.17.0.1:7890",
}

# ============================================================================
# 青龙面板通知 —— 自动加载青龙自带的 notify 模块
# ============================================================================
def load_notify():
    """尝试加载青龙面板自带的 notify 模块，返回 send 函数。"""
    candidates = [
        "/ql/scripts",
        "/ql/data/scripts",
        os.environ.get("QL_DIR", ""),
    ]
    for p in candidates:
        if p and p not in sys.path:
            sys.path.append(p)
    try:
        from notify import send
        return send
    except ImportError:
        pass
    # 部分新版青龙把 notify 放在 shell 子目录
    for p in ["/ql/shell", "/ql/data/shell"]:
        if p not in sys.path:
            sys.path.append(p)
    try:
        from notify import send
        return send
    except ImportError:
        return None

NOTIFY = load_notify()

SITEKEYS = {
    "tabitoken": "0x4AAAAAAEGV81TArluaPQGB",
    "tabi": "0x4AAAAAAEGV81TArluaPQGB",
    "gorouter": "0x4AAAAAAELziOpg1Y2gFtAt",
    "justwoker": "0x4AAAAAAELziOpg1Y2gFtAt",
    "justdowork": "0x4AAAAAAELziOpg1Y2gFtAt",
    "seekai": "0x4AAAAAAEC1XuxumkQrLCRB",
    "seek": "0x4AAAAAAEC1XuxumkQrLCRB",
}

BASE_URLS = {
    "tabitoken": "https://tabitoken.com",
    "tabi": "https://tabitoken.com",
    "gorouter": "https://gorouter.app",
    "justwoker": "https://api.justwoker.icu",
    "justdowork": "https://api.justwoker.icu",
    "seekai": "https://seekai.cc",
    "seek": "https://seekai.cc",
}

# Turnstile 验证码实际渲染的页面 URL（可能与 API base_url 不同）
# YesCaptcha 要求 websiteURL 必须与 Turnstile 渲染页面一致，否则解不出来
SITE_URLS = {
    "tabitoken": "https://tabitoken.com",
    "tabi": "https://tabitoken.com",
    "gorouter": "https://gorouter.app",
    "justwoker": "https://justwoker.icu",
    "justdowork": "https://justwoker.icu",
    "seekai": "https://seekai.cc",
    "seek": "https://seekai.cc",
}

# Turnstile 失败时的重试次数（token 可能过期，重新解一次）
TURNSTILE_MAX_RETRIES = 2


def solve_turnstile(yescaptcha_key, website_url, sitekey):
    """通过 YesCaptcha 解决 Cloudflare Turnstile，返回 token 或 None。"""
    if not yescaptcha_key or "YOUR_" in yescaptcha_key:
        return None
    payload = {
        "clientKey": yescaptcha_key,
        "task": {
            "type": "TurnstileTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": sitekey,
        },
    }
    try:
        r_task = requests.post(
            "https://api.yescaptcha.com/createTask",
            json=payload,
            timeout=10,
            proxies=PROXIES,
        ).json()
        task_id = r_task.get("taskId")
        if not task_id:
            return None
        for _ in range(12):
            time.sleep(1.5)
            res = requests.post(
                "https://api.yescaptcha.com/getTaskResult",
                json={"clientKey": yescaptcha_key, "taskId": task_id},
                timeout=10,
                proxies=PROXIES,
            ).json()
            if res.get("status") == "ready":
                return res.get("solution", {}).get("token")
            elif res.get("status") != "processing":
                break
    except Exception:
        pass
    return None


def run_account(acc, yescaptcha_key):
    """执行单个账号的签到流程，返回结果字符串。"""
    platform = acc.get("platform", "").lower()
    name = acc.get("name", platform)
    token = acc.get("token", "")
    user_id = acc.get("user_id")
    base_url = acc.get("url") or BASE_URLS.get(platform)
    # sitekey：优先用账号配置中的覆盖值，否则用默认映射
    sitekey = acc.get("sitekey") or SITEKEYS.get(platform)
    # site_url：Turnstile 实际渲染页面，优先用账号配置，否则用默认映射，最后回退到 base_url
    site_url = acc.get("site_url") or SITE_URLS.get(platform) or base_url
    if not base_url or not token:
        return f"[FAIL] [{name}] Missing base_url or token"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Origin": base_url,
        "Referer": f"{base_url}/",
    }
    if platform == "gorouter" and user_id:
        headers["New-Api-User"] = str(user_id)
    try:
        # 1. 查询签到状态
        r_check = requests.get(
            f"{base_url}/api/user/checkin", headers=headers, timeout=10, proxies=PROXIES
        )
        if r_check.status_code != 200:
            return f"[WARN] [{name}] Audit Status {r_check.status_code}"
        stats = r_check.json().get("data", {}).get("stats", {})
        if stats.get("checked_in_today"):
            return f"[INFO] [{name}] Already checked in today"

        # 2. 解 Turnstile + 签到（失败重试）
        for attempt in range(1, TURNSTILE_MAX_RETRIES + 1):
            ts_token = solve_turnstile(yescaptcha_key, site_url, sitekey)
            if not ts_token:
                if attempt < TURNSTILE_MAX_RETRIES:
                    time.sleep(2)
                    continue
                return (
                    f"[FAIL] [{name}] Turnstile solve failed"
                    f" (url={site_url}, key={sitekey})"
                )
            # 3. 提交签到
            r_claim = requests.post(
                f"{base_url}/api/user/checkin?turnstile={ts_token}",
                headers=headers,
                json={},
                timeout=10,
                proxies=PROXIES,
            ).json()
            if r_claim.get("success"):
                awarded = r_claim.get("data", {}).get("quota_awarded", 0)
                return f"[SUCCESS] [{name}] Claim Success: +{awarded:,} quota!"
            else:
                msg = r_claim.get("message", "")
                # Turnstile 校验失败可能是 token 过期，重试一次
                if "Turnstile" in msg or "turnstile" in msg.lower():
                    if attempt < TURNSTILE_MAX_RETRIES:
                        time.sleep(2)
                        continue
                return f"[WARN] [{name}] Claim Failed: {msg}"
        return f"[FAIL] [{name}] Turnstile retries exhausted"
    except Exception as e:
        return f"[FAIL] [{name}] Error: {e}"


def main():
    parser = argparse.ArgumentParser(
        description="Auto-Checkin Master CLI for GoRouter, Tabi AI, JustDoWork & SeekAI"
    )
    parser.add_argument(
        "-c",
        "--config",
        default="accounts.json",
        help="Path to accounts.json file (ignored when AI_API_ACCOUNTS env is set)",
    )
    args = parser.parse_args()

    # ========================================================================
    # 配置加载优先级：
    #   1. 环境变量 AI_API_ACCOUNTS（JSON 字符串，青龙环境变量管理）
    #   2. 环境变量 AI_API_CONFIG（指向 accounts.json 的路径）
    #   3. -c / --config 参数（默认 accounts.json 文件）
    # ========================================================================
    env_accounts_raw = os.environ.get("AI_API_ACCOUNTS", "").strip()

    if env_accounts_raw:
        # 从环境变量加载完整配置（含 yescaptcha_key 和 accounts）
        try:
            config = json.loads(env_accounts_raw)
        except json.JSONDecodeError as e:
            msg = f"[ERROR] AI_API_ACCOUNTS is not valid JSON: {e}"
            print(msg)
            if NOTIFY:
                NOTIFY("Auto-Checkin", msg)
            sys.exit(1)
        print("[*] Config loaded from env: AI_API_ACCOUNTS")
    else:
        # 从文件加载
        env_config_path = os.environ.get("AI_API_CONFIG", "").strip()
        config_path = Path(env_config_path or args.config)
        if not config_path.exists():
            msg = f"[ERROR] Config file '{config_path}' not found."
            print(msg)
            if NOTIFY:
                NOTIFY("Auto-Checkin", msg)
            sys.exit(1)
        with open(config_path) as f:
            config = json.load(f)
        print(f"[*] Config loaded from file: {config_path}")

    yes_key = config.get("yescaptcha_key", "")
    accounts = config.get("accounts", [])

    print(
        f"[*] Starting check-in for {len(accounts)} accounts at"
        f" {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if PROXIES:
        print(f"[*] Using proxy: {PROXIES['http']}")
    print()

    results = []
    for acc in accounts:
        result = run_account(acc, yes_key)
        print(result)
        results.append(result)
        time.sleep(1.0)

    print("\n[+] All accounts processed.")

    # ========================================================================
    # 发送青龙通知
    # ========================================================================
    if NOTIFY:
        # 统计成功/失败
        success = sum(1 for r in results if "[SUCCESS]" in r)
        info = sum(1 for r in results if "[INFO]" in r)
        warn = sum(1 for r in results if "[WARN]" in r)
        fail = sum(1 for r in results if "[FAIL]" in r)
        title = f"Auto-Checkin: {success}成功 {info}已签 {warn}警告 {fail}失败"
        content = "\n".join(results)
        try:
            NOTIFY(title, content)
            print("[*] Notification sent.")
        except Exception as e:
            print(f"[WARN] Notification failed: {e}")
    else:
        print("[*] QingLong notify module not found, skipping notification.")


if __name__ == "__main__":
    main()
