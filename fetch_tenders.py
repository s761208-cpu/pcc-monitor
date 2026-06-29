"""
標案狀態每日追蹤腳本
------------------------------------------------------------
讀取 Google Sheet 中列出的標案清單，針對「追蹤狀態」為「追蹤中」的案件，
向「標案瀏覽 API」(pcc-api.openfun.app，民間整理的政府電子採購網公開資料 API)
查詢最新公告（開標／決標／無法決標等），並把查到的結果寫回同一份 Google Sheet。

⚠️ 第一次執行前請看 SETUP.md，需要：
   1. 建立 GCP 服務帳戶並把金鑰存成 GitHub Secret：GOOGLE_SERVICE_ACCOUNT_JSON
   2. 把該服務帳戶的 email 加入 Google Sheet 的「共用」名單（給編輯權限）
   3. 在 GitHub Secret 設定 SHEET_ID（Google Sheet 的網址中那段 ID）

設計上的取捨（請依需要調整）：
- 只處理「追蹤狀態」欄位 = 追蹤中 的列，已經是「歷史」的案件不會再去查詢，省 API 次數。
- 因為原始 Sheet 並未記錄政府採購網的「機關代碼」「案號」，腳本第一次執行時會用
  「標案名稱」的片段去公開 API 搜尋比對「機關名稱」，自動把案號/機關代碼補回 Sheet
  的兩個新欄位（這樣下次查詢就能直接用案號精準比對，不用再猜）。
  這種比對是「盡力而為」，找不到或比對信心不足時會在「比對狀態」欄位註明，
  請人工確認、或直接把正確的案號/機關代碼手動填入對應欄位。
- 找到「決標公告」會自動填入得標廠商與得標金額，並把追蹤狀態改成「歷史」；
  找到「無法決標／廢標」也會記錄原因並改成「歷史」。
- 原本程式的「關鍵字搜尋新標案 + 寄送 Email」功能已移除（改成寫入 Google Sheet）。
  如果之後還想要「自動發現新案件」的功能，可以再加回來，告訴我就行。
"""

import os
import re
import json
import time
import difflib
from datetime import datetime, timezone, timedelta

import requests
import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# ✏️ 設定區
# ============================================================

# Google Sheet ID（網址 https://docs.google.com/spreadsheets/d/<這段>/edit 裡的那段）
SHEET_ID = os.environ.get("SHEET_ID", "1N_YIeCNN8R9XN_gMySdf9zAW0JzTzLHmBr1LK2hM0-0")

# 要操作的工作表（分頁）名稱；留空字串 = 用第一個分頁
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "")

# 政府採購網開放資料 API（標案瀏覽 / pcc-api.openfun.app）
PCC_API_BASE = "https://pcc-api.openfun.app/api"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; pcc-monitor-bot/1.0)",
    "Accept": "application/json",
}

# 比對「機關名稱」用的相似度門檻（0~1，越高越嚴格）
UNIT_MATCH_THRESHOLD = 0.55

# 每一列最多嘗試幾種搜尋關鍵字片段（避免打太多次 API）
MAX_QUERY_VARIANTS = 5

# 兩次 API 呼叫之間的小延遲（秒），對公開 API 客氣一點
REQUEST_DELAY = 0.4

TAIPEI_TZ = timezone(timedelta(hours=8))


# ============================================================
# 工具函式
# ============================================================

def now_str() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")


def normalize(s: str) -> str:
    """拿掉空白、全半形差異，方便比對。"""
    if not s:
        return ""
    s = s.strip()
    s = s.replace(" ", "").replace("　", "")
    return s


def unit_similarity(a: str, b: str) -> float:
    """機關名稱相似度（0~1）。"""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def title_query_variants(title: str, max_variants: int = MAX_QUERY_VARIANTS) -> list:
    """
    從標案名稱切出幾段候選關鍵字去查 API。
    searchbytitle 是「精確片語」比對，所以盡量挑長一點、獨特一點的片段，
    並從前段、中段各取一些，增加命中機會。
    """
    title = normalize(title)
    title = re.sub(r"[（(].*?[）)]", "", title)  # 拿掉括號內容（常見是備註，容易造成不匹配）
    if len(title) < 6:
        return [title] if title else []

    variants = []
    window = 10
    step = 6
    i = 0
    while i + window <= len(title) and len(variants) < max_variants:
        variants.append(title[i:i + window])
        i += step
    if not variants:
        variants.append(title[: min(window, len(title))])
    # 去重，保留順序
    seen = set()
    out = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out[:max_variants]


def pcc_search_by_title(query: str, page: int = 1) -> list:
    url = f"{PCC_API_BASE}/searchbytitle"
    try:
        resp = requests.get(url, params={"query": query, "page": page}, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("records", []) or []
    except Exception as e:
        print(f"[WARN] 搜尋失敗 query={query!r}: {e}")
        return []


def pcc_get_tender_timeline(unit_id: str, job_number: str) -> list:
    url = f"{PCC_API_BASE}/tender"
    try:
        resp = requests.get(url, params={"unit_id": unit_id, "job_number": job_number}, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        return data.get("records", []) or []
    except Exception as e:
        print(f"[WARN] 查詢案件歷程失敗 unit_id={unit_id} job_number={job_number}: {e}")
        return []


def find_job_for_row(unit_name: str, title: str):
    """
    用標案名稱片段 + 機關名稱比對，盡力找出 (unit_id, job_number, 信心分數)。
    找不到回傳 (None, None, 0)。
    """
    best = (None, None, 0.0, None)
    for q in title_query_variants(title):
        records = pcc_search_by_title(q)
        time.sleep(REQUEST_DELAY)
        for r in records:
            sim = unit_similarity(unit_name, r.get("unit_name", ""))
            if sim > best[2]:
                best = (r.get("unit_id"), r.get("job_number"), sim, r.get("date"))
        if best[2] >= 0.85:
            break  # 信心已經很高，不用再試其他關鍵字
    return best


AWARD_TYPE_HINTS = ["決標公告"]
FAILED_TYPE_HINTS = ["無法決標", "廢標", "流標", "停標"]


def classify_latest(records: list):
    """
    從某案件的歷程記錄中，取出日期最新的一筆，判斷目前狀態。
    回傳 dict: {status, type, date, url, winner, amount, reason}
    status 為其中之一: "awarded"（已決標）/ "failed"（無法決標等）/ "open"（仍在進行中）
    """
    if not records:
        return None

    latest = max(records, key=lambda r: r.get("date", 0))
    brief = latest.get("brief", {}) or {}
    detail = latest.get("detail", {}) or {}
    announce_type = brief.get("type", "") or detail.get("type", "")
    url = detail.get("url", "")

    result = {
        "status": "open",
        "type": announce_type,
        "date": latest.get("date"),
        "url": url,
        "winner": None,
        "amount": None,
        "reason": None,
    }

    if any(h in announce_type for h in FAILED_TYPE_HINTS):
        result["status"] = "failed"
        reason = None
        for k, v in detail.items():
            if "原因" in k and isinstance(v, str) and v.strip():
                reason = v.strip()
                break
        result["reason"] = reason or f"{announce_type}（詳情請見公告連結）"
        return result

    if any(h in announce_type for h in AWARD_TYPE_HINTS):
        result["status"] = "awarded"
        winners = []
        amount = None
        for k, v in detail.items():
            if isinstance(v, str) and k.endswith("得標廠商") and "未得標廠商" not in k and v.strip():
                if v.strip() not in winners:
                    winners.append(v.strip())
            if isinstance(v, str) and ("總決標金額" in k) and v.strip():
                amount = v.strip()
        if amount is None:
            # 後備方案：抓任何一個「決標金額」欄位
            for k, v in detail.items():
                if isinstance(v, str) and k.endswith("決標金額") and v.strip():
                    amount = v.strip()
                    break
        result["winner"] = "、".join(winners) if winners else None
        result["amount"] = amount
        return result

    return result


# ============================================================
# Google Sheets 存取
# ============================================================

EXTRA_COLUMNS = ["案號", "機關代碼", "比對狀態", "PCC最新公告類型", "最後查詢時間", "公告連結"]


def open_sheet():
    creds_raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not creds_raw:
        raise SystemExit(
            "[ERROR] 找不到環境變數 GOOGLE_SERVICE_ACCOUNT_JSON，"
            "請依 SETUP.md 設定 GitHub Secret 或本機環境變數。"
        )
    info = json.loads(creds_raw)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(WORKSHEET_NAME) if WORKSHEET_NAME else sh.sheet1
    return ws


def load_table(ws):
    """
    找出主表格的標題列與資料範圍（避免動到標題列下方的統計表）。
    回傳 (header_row_idx, header_list, data_rows)
    data_rows 為 list[(row_idx, dict)]，row_idx 為 1-based 的工作表列號。
    """
    all_values = ws.get_all_values()
    header_idx = None
    header = None
    for i, row in enumerate(all_values):
        if "機關名稱" in row and "標案名稱" in row:
            header_idx = i
            header = row
            break
    if header_idx is None:
        raise SystemExit("[ERROR] 在工作表中找不到含「機關名稱」「標案名稱」的標題列，請確認 Sheet 結構。")

    unit_col = header.index("機關名稱")
    rows = []
    for i in range(header_idx + 1, len(all_values)):
        row = all_values[i]
        if len(row) <= unit_col or not row[unit_col].strip():
            break  # 遇到空白列，視為主表格結束（後面是統計表）
        row_dict = {header[c]: (row[c] if c < len(row) else "") for c in range(len(header))}
        rows.append((i + 1, row_dict))  # +1 -> 轉成 1-based 列號
    return header_idx + 1, header, rows


def ensure_extra_columns(ws, header_row_idx: int, header: list):
    missing = [c for c in EXTRA_COLUMNS if c not in header]
    if not missing:
        return header
    new_header = header + missing
    start_col = len(header) + 1
    ws.update(
        range_name=gspread.utils.rowcol_to_a1(header_row_idx, start_col),
        values=[missing],
    )
    return new_header


def col_letter(header: list, name: str) -> str:
    idx = header.index(name) + 1
    return gspread.utils.rowcol_to_a1(1, idx).rstrip("1")


# ============================================================
# 主流程
# ============================================================

def main():
    ws = open_sheet()
    header_row_idx, header, rows = load_table(ws)
    header = ensure_extra_columns(ws, header_row_idx, header)

    print(f"[INFO] 讀到主表格共 {len(rows)} 列資料（標題列在第 {header_row_idx} 列）")

    updates = []  # 累積批次寫入: list of {"range": "A1", "values": [[...]]}
    checked, resolved, no_match = 0, 0, 0

    for row_idx, row in rows:
        status = (row.get("追蹤狀態") or "").strip()
        if status != "追蹤中":
            continue

        unit_name = (row.get("機關名稱") or "").strip()
        title = (row.get("標案名稱") or "").strip()
        job_number = (row.get("案號") or "").strip()
        unit_id = (row.get("機關代碼") or "").strip()

        print(f"[INFO] 第{row_idx}列：{unit_name} / {title}")

        match_note = row.get("比對狀態", "")

        if not job_number or not unit_id:
            found_unit_id, found_job, score, _ = find_job_for_row(unit_name, title)
            if found_unit_id and found_job and score >= UNIT_MATCH_THRESHOLD:
                unit_id, job_number = found_unit_id, found_job
                match_note = f"自動比對成功（相似度 {score:.2f}）"
            else:
                match_note = f"找不到可信的對應案件（最佳相似度 {score:.2f}），請人工確認或手動填入案號/機關代碼"
                no_match += 1
                updates.append({"range": f"{col_letter(header,'比對狀態')}{row_idx}", "values": [[match_note]]})
                updates.append({"range": f"{col_letter(header,'最後查詢時間')}{row_idx}", "values": [[now_str()]]})
                continue

        timeline = pcc_get_tender_timeline(unit_id, job_number)
        time.sleep(REQUEST_DELAY)
        checked += 1
        info = classify_latest(timeline)

        row_updates = {
            "案號": job_number,
            "機關代碼": unit_id,
            "比對狀態": match_note or "已有案號",
            "最後查詢時間": now_str(),
        }

        if info is None:
            row_updates["PCC最新公告類型"] = "查無公告紀錄"
        else:
            row_updates["PCC最新公告類型"] = info["type"] or ""
            row_updates["公告連結"] = info.get("url") or ""

            if info["status"] == "awarded":
                row_updates["得標廠商/不投標原因"] = info.get("winner") or "（已決標，廠商名稱未能自動解析，請查公告連結）"
                if info.get("amount"):
                    row_updates["得標金額(元)"] = info["amount"]
                row_updates["追蹤狀態"] = "歷史"
                resolved += 1
            elif info["status"] == "failed":
                row_updates["得標廠商/不投標原因"] = info.get("reason") or "無法決標"
                row_updates["追蹤狀態"] = "歷史"
                resolved += 1
            # status == "open" -> 維持「追蹤中」，只更新查詢時間/最新公告類型

        for col_name, value in row_updates.items():
            if col_name in header:
                updates.append({"range": f"{col_letter(header, col_name)}{row_idx}", "values": [[value]]})

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    print(f"[DONE] 本次查詢 {checked} 案，新解決 {resolved} 案，比對失敗 {no_match} 案。")


if __name__ == "__main__":
    main()
