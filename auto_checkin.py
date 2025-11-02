#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动抽奖签到 + 兑换 + 通知 + 生成 GitHub Pages 报表（单文件）
依赖：requests；Python 3.11+
作者：你
用法：在本地或 GitHub Actions 里运行。所有敏感参数经环境变量注入，避免泄露。
"""

import os
import re
import json
import time
import html
import pathlib
import datetime as dt
from typing import List, Dict, Any, Optional, Tuple

import requests
from zoneinfo import ZoneInfo


# --------------------------------
# 配置（全部从环境变量读取）
# --------------------------------

# 【抽奖】必要 Cookie & 头
LUCKYDRAW_COOKIE = os.getenv("LUCKYDRAW_COOKIE", "").strip()
# Next.js Server Action 标识（平台经常变，抓包后放 secrets）
LUCKYDRAW_NEXT_ACTION = os.getenv("LUCKYDRAW_NEXT_ACTION", "").strip()
# Next Router State（同上）
LUCKYDRAW_NEXT_ROUTER_STATE = os.getenv("LUCKYDRAW_NEXT_ROUTER_STATE", "").strip()

# 【兑换】必要 Cookie & 头
TOPUP_COOKIE = os.getenv("TOPUP_COOKIE", "").strip()
TOPUP_NEW_API_USER = os.getenv("TOPUP_NEW_API_USER", "").strip()  # new-api-user 头

# 用户代理可自定义
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
)

# 每天最多抽奖次数（平台说 5 次）
MAX_TIMES = int(os.getenv("MAX_TIMES", "5"))

# 推送（pluspush 或任意 Webhook）：留空则不推送
PUSH_URL = os.getenv("PLUSPUSH_URL", "https://www.pushplus.plus/api/send").strip()  # e.g. https://www.pushplus.plus/api/send
PUSH_TOKEN = os.getenv("PLUSPUSH_TOKEN", "").strip()
PUSH_CHANNEL = os.getenv("PLUSPUSH_CHANNEL", "").strip()  # 若有频道/设备标识
PUSH_TEMPLATE = os.getenv("PLUSPUSH_TEMPLATE", "markdown")  # markdown / text 等

# 其他
TIMEZONE = "Asia/Singapore"
DIST_DIR = pathlib.Path("dist")
REPORT_HTML = DIST_DIR / "index.html"
REPORT_JSON = DIST_DIR / "summary.json"

# 断点续跑状态（由 Actions cache 持久化）
STATE_DIR = pathlib.Path(os.getenv("STATE_DIR", ".runner_cache"))
STATE_FILE = STATE_DIR / "state.json"

SESSION_TIMEOUT = 30  # 网络超时秒


# --------------------------------
# 工具函数
# --------------------------------

def mask_code(code: str) -> str:
    if not code or len(code) < 8:
        return "****"
    return f"{code[:4]}****{code[-4:]}"


def now_sgt() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(TIMEZONE))


def ensure_dist():
    DIST_DIR.mkdir(parents=True, exist_ok=True)


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, Any]):
    ensure_state_dir()
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def today_key() -> str:
    return now_sgt().strftime("%Y-%m-%d")  # 以新加坡时区为准


def get_today_counts(state: Dict[str, Any]) -> int:
    return int(state.get("days", {}).get(today_key(), {}).get("tries", 0))


def bump_today_count(state: Dict[str, Any], inc: int = 1):
    days = state.setdefault("days", {})
    d = days.setdefault(today_key(), {"tries": 0})
    d["tries"] = int(d.get("tries", 0)) + inc
    save_state(state)


def post_json(url: str, json_body: Any, headers: Dict[str, str], cookies: Dict[str, str]) -> requests.Response:
    return requests.post(url, json=json_body, headers=headers, cookies=cookies, timeout=SESSION_TIMEOUT)


def cookie_string_to_dict(cookie_str: str) -> Dict[str, str]:
    """
    将 curl -b 里那种 'k1=v1; k2=v2; ...' 的字符串转为 dict
    """
    jar = {}
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if "=" in pair:
            k, v = pair.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


def send_push(title: str, content: str) -> Tuple[bool, str]:
    """
    通用 webhook 推送。默认发 JSON：
    {
      "token": PUSH_TOKEN,
      "title": title,
      "content": content,
      "template": PUSH_TEMPLATE,
      "channel": PUSH_CHANNEL (可选)
    }
    你可按自己的 pluspush 接口在服务器端适配。
    """
    if not PUSH_URL or not PUSH_TOKEN:
        return False, "PUSH_URL or PUSH_TOKEN is empty; skip push."

    payload = {
        "token": PUSH_TOKEN,
        "title": title,
        "content": content,
        "template": PUSH_TEMPLATE,
    }
    if PUSH_CHANNEL:
        payload["channel"] = PUSH_CHANNEL

    try:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        r = requests.post(PUSH_URL, data=body, headers=headers, timeout=SESSION_TIMEOUT)
        ok = (200 <= r.status_code < 300)
        return ok, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def log_flow(stage: str, message: str = "") -> None:
    """
    控制台输出当前流程阶段，便于 CI 日志追踪。
    """
    prefix = f"[流程][{stage}]"
    print(f"{prefix} {message}" if message else prefix)


# --------------------------------
# 业务：抽奖 & 解析兑换码
# --------------------------------

def luckydraw_once() -> Tuple[Optional[str], str, Dict[str, Any]]:
    """
    调用 Next.js Server Actions 接口抽奖一次，返回 (兑换码, 说明, 原始解析字典)
    该接口返回是 RSC(text/x-component)，可能不是纯 JSON，因此做了兜底解析。
    """
    url = "https://tw.b4u.qzz.io/luckydraw"
    cookies = cookie_string_to_dict(LUCKYDRAW_COOKIE)

    headers = {
        "accept": "text/x-component",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "text/plain;charset=UTF-8",
        "origin": "https://tw.b4u.qzz.io",
        "referer": "https://tw.b4u.qzz.io/luckydraw",
        "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": USER_AGENT,
        # 关键头部
        "next-action": LUCKYDRAW_NEXT_ACTION,
        "next-router-state-tree": LUCKYDRAW_NEXT_ROUTER_STATE,
    }

    data_raw = '[{"excludeThankYou":false}]'

    try:
        r = requests.post(url, data=data_raw.encode("utf-8"), headers=headers, cookies=cookies, timeout=SESSION_TIMEOUT)
    except Exception as e:
        return None, f"请求异常：{e}", {}

    if r.status_code == 401:
        return None, "抽奖接口返回 401，疑似未登录或 Cookie 失效。", {"status": r.status_code}
    if r.status_code == 403:
        preview = r.text[:200]
        if "Just a moment" in preview:
            hint = "抽奖接口返回 403（Cloudflare 挑战），请更新 LUCKYDRAW_COOKIE/Next-Action 参数。"
        else:
            hint = "抽奖接口返回 403，可能 Cookie 或 next-action 参数已过期。"
        return None, hint, {"status": r.status_code, "preview": preview}
    if r.status_code >= 500:
        return None, f"抽奖接口服务器错误：HTTP {r.status_code}", {"status": r.status_code}

    try:
        text = r.content.decode("utf-8", errors="replace")
    except Exception:
        text = r.text
    redemption = None
    msg = ""
    parsed: Dict[str, Any] = {}

    # 1) 正则找 redemptionCode
    m = re.search(r'"redemptionCode"\s*:\s*"([0-9a-fA-F]{16,})"', text)
    if m:
        redemption = m.group(1)

    # 2) 提取 success/message
    m2 = re.search(r'"success"\s*:\s*(true|false)', text)
    m3 = re.search(r'"message"\s*:\s*"(.*?)"', text)
    if m3:
        msg = html.unescape(m3.group(1))

    # 3) 抠出 JSON 片段尝试 loads
    try:
        obj_parts = re.findall(r'\{.*?\}', text, flags=re.S)
        for part in obj_parts[::-1]:  # 从后往前，通常最后一个更完整
            try:
                parsed = json.loads(part)
                break
            except Exception:
                continue
        if not redemption and parsed and isinstance(parsed, dict):
            redemption = parsed.get("redemptionCode")
        if not msg and parsed and isinstance(parsed, dict):
            msg = parsed.get("message") or ""
    except Exception:
        pass

    if not msg:
        msg = f"HTTP {r.status_code}"

    return redemption, msg, parsed


# --------------------------------
# 业务：兑换
# --------------------------------

def topup_once(code: str) -> Tuple[bool, str, Optional[int]]:
    """
    用兑换码充值。返回 (成功与否, 文本说明, data 数值[若有])
    """
    url = "https://b4u.qzz.io/api/user/topup"
    cookies = cookie_string_to_dict(TOPUP_COOKIE)

    headers = {
        "accept": "application/json, text/plain, */*",
        "cache-control": "no-store",
        "content-type": "application/json",
        "origin": "https://b4u.qzz.io",
        "referer": "https://b4u.qzz.io/console/topup",
        "user-agent": USER_AGENT,
        "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
        "sec-ch-ua-arch": '"arm"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version": '"142.0.7444.60"',
        "sec-ch-ua-full-version-list": '"Chromium";v="142.0.7444.60", "Google Chrome";v="142.0.7444.60", "Not_A Brand";v="99.0.0.0"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-platform-version": '"26.1.0"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "new-api-user": TOPUP_NEW_API_USER,  # 关键头
    }

    body = {"key": code}
    try:
        r = post_json(url, body, headers, cookies)
    except Exception as e:
        return False, f"兑换请求异常：{e}", None

    if r.status_code >= 400:
        preview = r.text[:200]
        return False, f"兑换接口异常：HTTP {r.status_code} {preview}", None

    try:
        raw_text = r.content.decode("utf-8", errors="replace")
    except Exception:
        raw_text = r.text

    try:
        j = json.loads(raw_text)
    except Exception as e:
        return False, f"兑换响应解析失败：{e}", None

    ok = bool(j.get("success"))
    data_val = j.get("data")
    msg = j.get("message", "")
    return ok, msg, data_val


# --------------------------------
# 报表 & 页面
# --------------------------------

def render_html(report_date: str, items: List[Dict[str, Any]], total_amount: int) -> str:
    rows = []
    for i, it in enumerate(items, 1):
        rows.append(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td>{html.escape(it['draw_msg'])}</td>"
            f"<td>{html.escape(it['code_mask'])}</td>"
            f"<td>{'✅' if it['topup_ok'] else '❌'}</td>"
            f"<td>{it.get('topup_amount','')}</td>"
            f"<td>{html.escape(it.get('topup_msg',''))}</td>"
            f"</tr>"
        )
    table = "\n".join(rows)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日签到兑换（脱敏） - {html.escape(report_date)}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;padding:24px;background:#fafafa;color:#222}}
h1{{margin:0 0 8px}}
h2{{margin:0 0 16px;font-weight:500;color:#555}}
.card{{background:#fff;border-radius:16px;box-shadow:0 6px 18px rgba(0,0,0,.06);padding:20px;max-width:1000px}}
table{{width:100%;border-collapse:collapse}}
th,td{{border-bottom:1px solid #eee;padding:10px 8px;text-align:left;font-size:14px;vertical-align:top}}
th{{background:#fafafa}}
code.badge{{background:#eef;border-radius:10px;padding:2px 8px}}
footer{{margin-top:20px;color:#777;font-size:12px}}
.small{{color:#777;font-size:12px}}
</style>
</head>
<body>
<div class="card">
  <h1>每日签到兑换（脱敏）</h1>
  <h2>{html.escape(report_date)}</h2>
  <p>总兑换额：<code class="badge">{total_amount}</code></p>
  <p class="small">今日目标：5 次；页面展示为本次运行结果（历史可扩展为累计持久化）。时区：{html.escape(TIMEZONE)}</p>
  <table>
    <thead>
      <tr><th>#</th><th>抽奖提示</th><th>兑换码（脱敏）</th><th>兑换成功</th><th>兑换额</th><th>兑换信息</th></tr>
    </thead>
    <tbody>
      {table}
    </tbody>
  </table>
  <footer>自动生成 · 敏感信息均已脱敏</footer>
</div>
</body>
</html>
"""


# --------------------------------
# 主流程（含断点续跑）
# --------------------------------

def main():
    ensure_dist()
    ensure_state_dir()
    state = load_state()

    log_flow("签到", "初始化环境变量与状态缓存")

    # 基础校验
    missing = []
    if not LUCKYDRAW_COOKIE: missing.append("LUCKYDRAW_COOKIE")
    if not LUCKYDRAW_NEXT_ACTION: missing.append("LUCKYDRAW_NEXT_ACTION")
    if not LUCKYDRAW_NEXT_ROUTER_STATE: missing.append("LUCKYDRAW_NEXT_ROUTER_STATE")
    if not TOPUP_COOKIE: missing.append("TOPUP_COOKIE")
    if not TOPUP_NEW_API_USER: missing.append("TOPUP_NEW_API_USER")

    if missing:
        raise SystemExit(f"缺少必要环境变量：{', '.join(missing)}")

    already = get_today_counts(state)
    target = min(MAX_TIMES, 5)
    need = max(0, target - already)

    log_flow("签到", f"今日已尝试 {already}/{target} 次，计划追加 {need} 次抽奖")
    print(f"[Info] 今日已尝试 {already}/{target} 次，本次计划再跑 {need} 次。")

    results: List[Dict[str, Any]] = []

    for i in range(need):
        log_flow("抽奖", f"开始第 {i + 1} 次抽奖")
        # 抽奖
        code, draw_msg, parsed = luckydraw_once()

        # 无论成功与否，计作一次尝试（如需改为“拿到兑换码才算”，则把此行下移到 if code 块里）
        bump_today_count(state, inc=1)

        time.sleep(1.2)

        record: Dict[str, Any] = {
            "draw_msg": draw_msg or "",
            "code": code or "",
            "code_mask": mask_code(code) if code else "",
            "topup_ok": False,
            "topup_amount": "",
            "topup_msg": "",
        }

        if code:
            masked = mask_code(code)
            log_flow("抽奖", f"第 {i + 1} 次获得兑换码 {masked}")
            log_flow("兑换", f"准备兑换码 {masked}")
            ok, msg, amount = topup_once(code)
            record["topup_ok"] = ok
            record["topup_amount"] = amount if amount is not None else ""
            record["topup_msg"] = msg or ""
            log_flow("兑换", f"第 {i + 1} 次兑换{'成功' if ok else '失败'}：{msg or '无返回信息'}")
        else:
            reason = draw_msg if draw_msg else "未获取到兑换码"
            record["topup_msg"] = f"未获取到兑换码（{reason}）" if draw_msg else reason
            log_flow("抽奖", f"第 {i + 1} 次未获取兑换码：{reason}")

        results.append(record)
        time.sleep(1.2)

    # 汇总（本次运行的结果；如需累计历史，请把 results 存回 state 并在此读取合并）
    total_amount = sum(int(r["topup_amount"]) for r in results if isinstance(r.get("topup_amount"), int))

    today_str = now_sgt().strftime("%Y-%m-%d %H:%M:%S")
    summary = {
        "date": today_str,
        "timezone": TIMEZONE,
        "total_amount": total_amount,
        "items": results,
        "today_tried": get_today_counts(state),
        "today_target": target,
    }
    REPORT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_HTML.write_text(render_html(today_str, results, total_amount), encoding="utf-8")
    log_flow("汇总", "已生成 dist/index.html 与 dist/summary.json")

    # 推送（脱敏）
    has_success = any((r.get("topup_ok") or r.get("code")) for r in results)
    if not results or not has_success:
        log_flow("通知", "跳过推送，本次未成功签到/兑换")
        return

    title = f"签到兑换完成（{today_str}）"
    lines = [
        f"- 第{i+1}次：{'✅' if r['topup_ok'] else '❌'} {r['code_mask']} 额:{r.get('topup_amount','')}  {r.get('topup_msg','') or r.get('draw_msg','')}"
        for i, r in enumerate(results)
    ]
    content = f"**总额**：`{total_amount}`\n\n" + "\n".join(lines) if results else "今日已达 5 次目标或无新增尝试。"
    ok, detail = send_push(title, content)
    log_flow("通知", f"推送完成，状态={'成功' if ok else '失败'}")
    print(f"[Push] ok={ok} detail={detail[:200]}")


if __name__ == "__main__":
    main()
