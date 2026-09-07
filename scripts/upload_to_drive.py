"""把檔案上傳到 Google Drive 的一個資料夾。

排程每天產生一份 Excel。原本它存成 GitHub 的 artifact——那份東西 30 天後就
下載不到，而且要登入 GitHub 才拿得到。搬到 Drive 之後：不過期、可以直接分享、
而且每一天的檔案都留著。

## 需要什麼

* `GDRIVE_SA_KEY`（secret）：Google Cloud 服務帳號的金鑰 JSON，整份貼進去。
* `GDRIVE_FOLDER_ID`（variable）：要上傳到哪個資料夾。從資料夾網址的最後一段取，
  而且那個資料夾要**分享給服務帳號的信箱**（編輯者），否則會拿到 404——服務
  帳號看不到的資料夾，對它來說就是不存在。

## 為什麼是服務帳號，不是 OAuth

OAuth 的 refresh token 會過期、會被撤銷，而且第一次要有人在瀏覽器點同意。
排程沒有瀏覽器也沒有人。服務帳號是「給程式用的身分」，設定一次就不用再管。

服務帳號自己的 Drive 空間是 0，所以**一定要**上傳到分享給它的資料夾裡
（`parents`），不能傳到它的根目錄——後者會得到一個看起來像權限問題的配額錯誤。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

#: 從使用者填的東西裡取出真正的資料夾 id。
#:
#: Drive 的「複製連結」給的是
#: `https://drive.google.com/drive/folders/1AbC...xyz?usp=drive_link`，
#: 而設定欄位叫「FOLDER_ID」——於是最自然的動作是把網址最後一段貼進去，
#: 連著 `?usp=drive_link` 一起。
#:
#: 那樣拼出來的查詢是 `'1AbC...xyz?usp=drive_link' in parents`，Drive 回 404
#: 「File not found」——一個看起來像權限問題、其實是多了 17 個字元的錯誤。
#: 實際發生過。
#:
#: 所以這裡接受三種寫法：完整網址、id 加問號參數、乾淨的 id。
_ID = re.compile(r"[A-Za-z0-9_-]{10,}")


def folder_id(raw: str) -> str:
    """`https://…/folders/ABC?usp=drive_link`、`ABC?usp=…`、`ABC` → `ABC`。"""
    text = (raw or "").strip().strip("/")
    if not text:
        return ""
    if "/folders/" in text:
        text = text.split("/folders/", 1)[1]
    # 問號之後是參數，井字號之後是錨點——兩個都不是 id 的一部分。
    text = text.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
    m = _ID.fullmatch(text)
    return text if m else ""


def _sa_email(key: str) -> str:
    """從金鑰 JSON 取出服務帳號的信箱，只為了把它印在錯誤訊息裡。

    「分享給服務帳號」是最常漏的一步，而那個信箱藏在一份使用者多半沒有打開過的
    JSON 裡——讓錯誤訊息自己把它講出來，比叫人去翻檔案快。
    """
    try:
        return json.loads(key).get("client_email", "(讀不到)")
    except Exception:  # noqa: BLE001
        return "(金鑰不是有效的 JSON)"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：upload_to_drive.py <檔案> [<檔案>...]", file=sys.stderr)
        return 2

    key = os.environ.get("GDRIVE_SA_KEY", "").strip()
    raw_folder = os.environ.get("GDRIVE_FOLDER_ID", "")
    folder = folder_id(raw_folder)
    if raw_folder.strip() and not folder:
        print(
            f"::error::GDRIVE_FOLDER_ID 看不出資料夾 id：{raw_folder!r}\n"
            "應該是資料夾網址 /folders/ 後面那一段。",
            file=sys.stderr,
        )
        return 1
    if folder and folder != raw_folder.strip():
        print(f"  （GDRIVE_FOLDER_ID 去掉多餘的部分：{raw_folder.strip()!r} → {folder!r}）")
    if not key or not folder:
        # 沒設定就是沒設定，不是錯誤——這樣 fork 出去的人不必先去申請一組
        # Google 憑證才能跑排程。
        print("  （沒有 GDRIVE_SA_KEY / GDRIVE_FOLDER_ID，跳過上傳）")
        return 0

    from google.oauth2 import service_account  # noqa: PLC0415
    from googleapiclient.discovery import build  # noqa: PLC0415
    from googleapiclient.http import MediaFileUpload  # noqa: PLC0415

    creds = service_account.Credentials.from_service_account_info(
        json.loads(key), scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    for raw in argv[1:]:
        path = Path(raw)
        if not path.is_file():
            print(f"  （{path} 不在，跳過）")
            continue
        # 同名的先找出來：同一天重跑排程的時候要**覆蓋**，不是在資料夾裡堆出
        # 第二個一模一樣的名字。Drive 允許同名檔案共存，所以不處理的話那個
        # 資料夾會慢慢變成一堆看不出差別的重複檔。
        q = (
            f"name = '{path.name}' and '{folder}' in parents and trashed = false"
        )
        try:
            found = (
                drive.files()
                .list(q=q, fields="files(id)", pageSize=1)
                .execute()
                .get("files", [])
            )
        except Exception as exc:  # noqa: BLE001
            # Drive 對「不存在」和「你沒有權限看」回的是同一個 404。兩種原因
            # 各有一句話能講清楚，而原始的 traceback 兩句都沒講。
            if "404" in str(exc) or "notFound" in str(exc):
                print(
                    f"::error::Drive 找不到資料夾 {folder}。兩個可能：\n"
                    "  1. id 不對——它是資料夾網址 /folders/ 後面那一段，"
                    "不含 ?usp=drive_link\n"
                    f"  2. 資料夾沒有分享給服務帳號（{_sa_email(key)}），"
                    "權限要給編輯者\n"
                    "     服務帳號看不到的資料夾，對它來說就是不存在。",
                    file=sys.stderr,
                )
                return 1
            raise
        media = MediaFileUpload(
            str(path),
            mimetype="application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet",
            resumable=True,
        )
        if found:
            file_id = found[0]["id"]
            drive.files().update(fileId=file_id, media_body=media).execute()
            action = "覆蓋"
        else:
            file_id = (
                drive.files()
                .create(
                    body={"name": path.name, "parents": [folder]},
                    media_body=media,
                    fields="id",
                )
                .execute()["id"]
            )
            action = "新增"
        size_mb = path.stat().st_size / 1024 / 1024
        print(f"  {action} {path.name}（{size_mb:.1f} MB）→ {file_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
