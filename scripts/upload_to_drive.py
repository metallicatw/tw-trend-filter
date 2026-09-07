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
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：upload_to_drive.py <檔案> [<檔案>...]", file=sys.stderr)
        return 2

    key = os.environ.get("GDRIVE_SA_KEY", "").strip()
    folder = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
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
        found = (
            drive.files()
            .list(q=q, fields="files(id)", pageSize=1)
            .execute()
            .get("files", [])
        )
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
