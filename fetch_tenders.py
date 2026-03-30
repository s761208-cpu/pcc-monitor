"""
電子採購標案監控腳本
每天自動抓取符合條件的標案，並寄送 Email 通知
資料來源：政府電子採購網官方開放資料 + 政府資料開放平台
"""

import os
import re
import json
import smtplib
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ============================================================
# ✏️  設定區：依你的需求修改以下內容
# ============================================================

KEYWORDS = [
    "建築", "裝修", "裝潢", "圖書館", "宿舍", "教室", 
    "辦公室", "廠房", "廠辦", "科學園區", "台中",
    "污水", "聯通管", "下水道", "自來水", "給排水", 
    "水利", "抽水", "淨水", "統包", "礫間", 
    "景觀", "水環境", "改善工程", "校舍", "河川", 
    "水資源", "滯洪池", "停車場", "送水管", "輸水管", 
    "新建工程", "整建工程", "堤防", "疏濬", "管線工程", 
    "用戶接管"
]

TARGET_UNITS = []   # 空 = 全台灣不限機關
MIN_AMOUNT   = 0    # 最低金額，0 = 不限

# ============================================================
# Email 設定（從 GitHub Secrets 讀取）
# ============================================================
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_TO  = os.environ.get("EMAIL_TO", "")

# ============================================================

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
}


def fetch_from_data_gov(target_date: str) -> list[dict]:
    """從政府資料開放平台抓取採購資料（最穩定）"""
    tenders = []
    # 政府資料開放平台 - 採購公告資料集
    urls = [
        "https://data.gov.tw/api/v2/rest/datastore/049_540001?limit=1000",
        "https://data.gov.tw/api/v2/rest/datastore/049_540002?limit=1000",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                records = data.get("result", {}).get("records", [])
                print(f"[INFO] 開放平台回傳 {len(records)} 筆（{url[-20:]}）")
                for r in records:
                    # 嘗試多種欄位名稱（不同資料集格式不同）
                    title  = (r.get("標案名稱") or r.get("tender_name") or r.get("tenderName") or "").strip()
                    unit   = (r.get("機關名稱") or r.get("org_name") or r.get("orgName") or "").strip()
                    amount = (r.get("預算金額") or r.get("budget_amount") or r.get("budgetAmount") or "0").strip()
                    dt     = (r.get("公告日期") or r.get("publish_date") or r.get("publishDate") or "").strip()
                    pk     = (r.get("pkPmsMain") or "").strip()
                    link   = f"https://web.pcc.gov.tw/prkms/tender/common/basic/readTenderBasic?pkPmsMain={pk}" if pk else ""
                    if title:
                        tenders.append({"title": title, "unit": unit, "amount": amount, "date": dt, "link": link})
        except Exception as e:
            print(f"[WARN] 開放平台失敗：{e}")
    return tenders


def fetch_from_pcc_xml() -> list[dict]:
    """從電子採購網官方 XML 抓取"""
    tenders = []
    xml_url = "https://web.pcc.gov.tw/prkms/tender/common/bulletion/indexBulletion"
    try:
        req = urllib.request.Request(xml_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            root = ET.fromstring(resp.read())
        for item in root.iter("tender"):
            title  = (item.findtext("tenderName") or "").strip()
            unit   = (item.findtext("orgName") or "").strip()
            amount = (item.findtext("budgetAmount") or "0").strip()
            dt     = (item.findtext("publishDate") or "").strip()
            pk     = (item.findtext("pkPmsMain") or "").strip()
            link   = f"https://web.pcc.gov.tw/prkms/tender/common/basic/readTenderBasic?pkPmsMain={pk}" if pk else ""
            if title:
                tenders.append({"title": title, "unit": unit, "amount": amount, "date": dt, "link": link})
        print(f"[INFO] XML 回傳 {len(tenders)} 筆")
    except Exception as e:
        print(f"[WARN] XML 失敗：{e}")
    return tenders


def fetch_tenders(target_date: str) -> list[dict]:
    """依序嘗試各資料來源"""
    tenders = fetch_from_data_gov(target_date)
    if not tenders:
        tenders = fetch_from_pcc_xml()
    return tenders


def clean_amount(s: str) -> int:
    try:
        return int(re.sub(r"[^\d]", "", str(s)) or 0)
    except Exception:
        return 0


def match_tender(t: dict) -> bool:
    title  = t.get("title", "") or ""
    unit   = t.get("unit",  "") or ""
    amount = clean_amount(t.get("amount", "0"))
    kw_ok  = any(kw in title for kw in KEYWORDS) if KEYWORDS else True
    un_ok  = any(u in unit   for u in TARGET_UNITS) if TARGET_UNITS else True
    am_ok  = amount >= MIN_AMOUNT
    return kw_ok and un_ok and am_ok


def build_html_email(tenders: list[dict], target_date: str) -> str:
    rows = ""
    for t in tenders:
        title  = t.get("title", "（無標題）")
        unit   = t.get("unit",  "—")
        amount = t.get("amount","—")
        dt     = t.get("date",  "—")
        link   = t.get("link",  "")
        cell   = f'<a href="{link}" target="_blank">{title}</a>' if link else title
        rows  += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd;">{cell}</td>
          <td style="padding:8px;border:1px solid #ddd;">{unit}</td>
          <td style="padding:8px;border:1px solid #ddd;text-align:right;">{amount}</td>
          <td style="padding:8px;border:1px solid #ddd;">{dt}</td>
        </tr>"""
    return f"""
    <html><body style="font-family:Arial,sans-serif;color:#333;">
      <h2 style="color:#1a56db;">📋 電子採購標案日報 — {target_date}</h2>
      <p>共找到 <strong>{len(tenders)}</strong> 筆符合條件的標案</p>
      <table style="border-collapse:collapse;width:100%;font-size:14px;">
        <thead><tr style="background:#1a56db;color:#fff;">
          <th style="padding:10px;text-align:left;">標案名稱</th>
          <th style="padding:10px;text-align:left;">機關</th>
          <th style="padding:10px;text-align:right;">金額（元）</th>
          <th style="padding:10px;text-align:left;">公告日期</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#888;font-size:12px;margin-top:20px;">
        資料來源：政府電子採購網 / 政府資料開放平台<br>
        此信件由 GitHub Actions 自動發送
      </p>
    </body></html>"""


def send_email(subject: str, html_body: str):
    if not SMTP_USER or not SMTP_PASS or not EMAIL_TO:
        print("[WARN] Email 設定不完整，略過寄送")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo(); server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, EMAIL_TO.split(","), msg.as_string())
    print(f"[OK] Email 已寄出至 {EMAIL_TO}")


def main():
    target_date = (date.today() - timedelta(days=0)).strftime("%Y-%m-%d")
    print(f"[INFO] 查詢日期：{target_date}")
    all_tenders = fetch_tenders(target_date)
    print(f"[INFO] 共取得 {len(all_tenders)} 筆")
    matched = [t for t in all_tenders if match_tender(t)]
    print(f"[INFO] 篩選後符合 {len(matched)} 筆")
    if not matched:
        print("[INFO] 無符合標案，不寄送 Email")
        return
    subject = f"【採購標案日報】{target_date} — 共 {len(matched)} 筆符合標案"
    send_email(subject, build_html_email(matched, target_date))


if __name__ == "__main__":
    main()
