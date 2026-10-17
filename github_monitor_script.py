# -*- coding: utf-8 -*-
"""
cron: 0 */1 * * *
new Env('GitHub项目更新监控-按仓库汇总版');
"""

import json
import os
import sys
from datetime import datetime, timedelta

import requests

# ==================== 配置区 ====================
# GitHub Token（可选；建议使用环境变量，避免把 Token 写入脚本）
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# 待监控的仓库列表，格式：用户名/仓库名
REPOS = [
    "BuildAtDawn/qq-farm-bot",
    "aaang99/qq-farm-bot-404",
    "xxxscarlxrd404/qq-farm-bot",
    "liyangpengs/qq-farm-bot",
    "develop202/kgcheckin",
    "bia-pain-bache/BPB-Worker-Panel",
    "cmliu/edgetunnel",
    "lxpkwwz/ql_scripts",
    #"huojiujian2/feiland-idle-game",#费兰德世界
]

# 状态记录文件
STATE_FILE = "/ql/data/config/github_monitor_stable.json"
# ===============================================

def _prepare_ql_notify_paths():
    """加入常见青龙通知模块路径，兼容不同青龙目录布局。"""
    paths = [os.path.dirname(os.path.abspath(__file__))]
    ql_dir = os.environ.get("QL_DIR")
    if ql_dir:
        paths.append(os.path.join(ql_dir, "scripts"))
    paths.extend(["/ql/scripts", "/ql/data/scripts"])
    for path in paths:
        if path and os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)


def ql_notify(title, content):
    """优先调用青龙面板系统通知，不再使用模拟通知输出。"""
    _prepare_ql_notify_paths()
    errors = []

    # 青龙常见 notify.py：优先 send，其次兼容 send_notify。
    try:
        import notify
        for name in ("send", "send_notify"):
            sender = getattr(notify, name, None)
            if callable(sender):
                try:
                    sender(title, content)
                    print(f"\n✅ 已通过青龙系统通知发送：{title}")
                    return True
                except Exception as exc:
                    errors.append(f"notify.{name}: {exc}")
    except Exception as exc:
        errors.append(f"notify: {exc}")

    # 兼容旧版 sendNotify.py 的类、实例方法或函数形式。
    try:
        import sendNotify
        sender = getattr(sendNotify, "sendNotify", None)
        if callable(sender):
            try:
                sender().send(title, content)
            except (AttributeError, TypeError):
                sender(title, content)
            print(f"\n✅ 已通过青龙系统通知发送：{title}")
            return True
    except Exception as exc:
        errors.append(f"sendNotify: {exc}")

    print("\n⚠️ 未找到可用的青龙系统通知模块，请检查青龙通知配置。")
    if errors:
        print("通知诊断：" + " | ".join(errors[-2:]))
    return False


def format_time(utc_time_str):
    """将 GitHub 返回的 UTC 时间转换为北京时间。"""
    try:
        utc_dt = datetime.strptime(utc_time_str, "%Y-%m-%dT%H:%M:%SZ")
        return (utc_dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return utc_time_str.replace("T", " ").replace("Z", "")


def get_latest_commits(repo):
    """获取仓库默认分支最近 30 条提交。"""
    url = f"https://api.github.com/repos/{repo}/commits?per_page=30"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "QingLong-GitHub-Monitor",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"❌ 获取仓库 [{repo}] 提交失败：{exc}")
        return []


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"⚠️ 读取状态文件失败，将按空状态处理：{exc}")
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    temp_file = f"{STATE_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temp_file, STATE_FILE)


def build_repo_block(repo, commits):
    """生成一个仓库的汇总区块；只保留最后一个提交的详情链接。"""
    lines = [
        f"📦 仓库：{repo}",
        f"📢 新提交：{len(commits)} 个",
        "",
    ]

    for index, commit in enumerate(commits, start=1):
        commit_data = commit.get("commit", {})
        author_data = commit_data.get("author") or {}
        sha = commit.get("sha", "")[:7]
        author = author_data.get("name", "未知作者")
        commit_time = format_time(author_data.get("date", ""))
        message = commit_data.get("message", "").splitlines()[0].strip() or "无提交说明"

        lines.extend([
            f"{index}. 🔁 Commit：{sha}",
            f"   👤 作者：{author}",
            f"   🕐 时间：{commit_time} (北京时间)",
            f"   📝 信息：{message}",
        ])

    # commits 已按旧到新排列，因此最后一条就是最终修改对应的提交。
    final_url = commits[-1].get("html_url", "")
    lines.extend([
        "",
        f"🔗 最终修改：{final_url}",
        "----------------------------------------",
    ])
    return "\n".join(lines)


def main():
    print("=" * 60)
    print(f"🚀 GitHub 仓库更新监控 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if GITHUB_TOKEN:
        print("🔑 已配置 GitHub Token，速率限制 5000 次/小时")
    else:
        print("⚠️ 未配置 GitHub Token，速率限制 60 次/小时（建议配置）")
    print(f"📊 本次监控仓库数：{len(REPOS)}")
    print("=" * 60)

    state = load_state()
    updated_state = state.copy()
    repo_blocks = []
    total_new_commits = 0

    for repo in REPOS:
        print(f"\n🔍 检查项目：{repo}")
        commits = get_latest_commits(repo)
        if not commits:
            print("⚠️ 未获取到提交数据，保留原状态。")
            continue

        latest_sha = commits[0].get("sha", "")
        last_sha = state.get(repo)

        if not last_sha:
            updated_state[repo] = latest_sha
            print(f"🆕 首次运行，已记录最新提交：{latest_sha[:7]}")
            continue

        if latest_sha == last_sha:
            print(f"😴 无新更新（最新：{latest_sha[:7]}）")
            continue

        new_commits = []
        found_previous = False
        for commit in commits:
            if commit.get("sha") == last_sha:
                found_previous = True
                break
            new_commits.append(commit)

        # API 只返回最近 30 条时，如果找不到旧 SHA，避免重复推送全部历史记录。
        if not found_previous and len(new_commits) == len(commits):
            print("⚠️ 最近 30 条提交中未找到上次状态，跳过本次通知并更新状态，避免重复推送历史提交。")
            updated_state[repo] = latest_sha
            continue

        if new_commits:
            ordered_commits = list(reversed(new_commits))
            repo_block = build_repo_block(repo, ordered_commits)
            repo_blocks.append(repo_block)
            total_new_commits += len(ordered_commits)

            # 日志与通知使用完全相同的仓库汇总区块。
            print(f"\n{repo_block}")
            updated_state[repo] = latest_sha

    if repo_blocks:
        title = f"📢 GitHub 代码更新通知（共 {total_new_commits} 个新提交）"
        content = "\n".join(repo_blocks)
        ql_notify(title, content)
        print(f"\n✅ 已处理汇总通知：{len(repo_blocks)} 个仓库，{total_new_commits} 个提交")
    else:
        print("\n✨ 所有仓库均无新提交")

    save_state(updated_state)
    print("\n" + "=" * 60)
    print("🏁 监控结束，状态已保存")
    print("=" * 60)


if __name__ == "__main__":
    main()
