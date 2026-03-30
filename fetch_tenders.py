"""
電子採購標案監控腳本
每天自動抓取符合條件的標案，並寄送 Email 通知
"""

import os
import json
import smtplib
import urllib.request
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ============================================================
# ✏️  設定區：依你的需求修改以下內容
# ============================================================

# 關鍵字篩選（標案名稱包含任一關鍵字即符合）
KEYWORDS = [
    "建築",
    "裝修",
    "教室",
    "工程",
    "景觀",
    "環境",
    "水利",
    "建設",
    "土木",
    "裝潢",
]

# 機關名稱篩選（空清單 = 不限機關；有填則只抓符合的機關）
TARGET_UNITS = []  # 全台灣，不限機關

# 最低金額門檻（元），0 = 不限
MIN_AMOUNT = 0

# ============================================================
# Email 設定（從 GitHub Secrets 讀取，不要直接填密碼！）
# ============================================================
SMTP_HOST   = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT   = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER   = os.environ.get("SMTP_USER", "")   # 你的 Gmail
SMTP_PASS   = os.environ.get("SMTP_PASS", "")   # Gmail App Password
EMAIL_TO    = os.environ.get("EMAIL_TO", "")    # 收件者（可用逗號分隔多位）

# ============================================================


def fetch_tenders_by_date(target_date: str) -> list[dict]:
    """呼叫 g0v PCC API，取得指定日期的所有標案"""
    url = f"https://pcc.g0v.ronny.tw/api/date/{target_date}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("records", [])
    except Exception as e:
        print(f"[ERROR] API 呼叫失敗：{e}")
        return []


def match_tender(tender: dict) -> bool:
    """判斷標案是否符合篩選條件"""
    name = tender.get("brief", {}).get("title", "") or ""
    unit = tender.get("unit_name", "") or ""
    amount_str = tender.get("brief", {}).get("amount", "0") or "0"

    # 去除金額中的逗號
    try:
        amount = int(str(amount_str).replace(",", "").replace(" ", "") or 0)
    except ValueError:
        amount = 0

    # 關鍵字篩選
    keyword_match = any(kw in name for kw in KEYWORDS) if KEYWORDS else True

    # 機關篩選
    unit_match = any(u in unit for u in TARGET_UNITS) if TARGET_UNITS else True

    # 金額篩選
    amount_match = amount >= MIN_AMOUNT

    return keyword_match and unit_match and amount_match


def build_html_email(tenders: list[dict], target_date: str) -> str:
    """產生 HTML 格式的 Email 內容"""
    rows = ""
    for t in tenders:
        brief   = t.get("brief", {})
        title   = brief.get("title", "（無標題）")
        unit    = t.get("unit_name", "—")
        amount  = brief.get("amount", "—")
        date_s  = brief.get("date", "—")
        pk      = t.get("pk", "")
        link    = f"https://web.pcc.gov.tw/prkms/tender/common/basic/readTenderBasic?pkPmsMain={pk}" if pk else "#"

        rows += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd;"><a href="{link}" target="_blank">{title}</a></td>
          <td style="padding:8px;border:1px solid #ddd;">{unit}</td>
          <td style="padding:8px;border:1px solid #ddd;text-align:right;">{amount}</td>
          <td style="padding:8px;border:1px solid #ddd;">{date_s}</td>
        </tr>
        """

    html = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;">
      <h2 style="color:#1a56db;">📋 電子採購標案日報 — {target_date}</h2>
      <p>共找到 <strong>{len(tenders)}</strong> 筆符合條件的標案</p>
      <table style="border-collapse:collapse;width:100%;font-size:14px;">
        <thead>
          <tr style="background:#1a56db;color:#fff;">
            <th style="padding:10px;text-align:left;">標案名稱</th>
            <th style="padding:10px;text-align:left;">機關</th>
            <th style="padding:10px;text-align:right;">金額（元）</th>
            <th style="padding:10px;text-align:left;">公告日期</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#888;font-size:12px;margin-top:20px;">
        資料來源：政府電子採購網 / g0v PCC API<br>
        此信件由 GitHub Actions 自動發送
      </p>
    </body></html>
    """
    return html


def send_email(subject: str, html_body: str):
    """透過 SMTP 寄送 HTML Email"""
    if not SMTP_USER or not SMTP_PASS or not EMAIL_TO:
        print("[WARN] Email 設定不完整，略過寄送（請確認 GitHub Secrets 已設定）")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, EMAIL_TO.split(","), msg.as_string())
        print(f"[OK] Email 已寄出至 {EMAIL_TO}")
    except Exception as e:
        print(f"[ERROR] 寄送失敗：{e}")
        raise


def main():
    # 抓「昨天」的標案（API 資料通常有一天延遲）
    target_date = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"[INFO] 查詢日期：{target_date}")

    all_tenders = fetch_tenders_by_date(target_date)
    print(f"[INFO] API 回傳 {len(all_tenders)} 筆標案")

    matched = [t for t in all_tenders if match_tender(t)]
    print(f"[INFO] 篩選後符合 {len(matched)} 筆")

    if not matched:
        print("[INFO] 無符合標案，不寄送 Email")
        return

    subject   = f"【採購標案日報】{target_date} — 共 {len(matched)} 筆符合標案"
    html_body = build_html_email(matched, target_date)
    send_email(subject, html_body)


if __name__ == "__main__":
    main()
