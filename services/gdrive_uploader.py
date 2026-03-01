from __future__ import annotations

from pathlib import Path
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


SCOPES = ["https://www.googleapis.com/auth/drive"]


def _build_service(credentials_file: Path):
    creds = service_account.Credentials.from_service_account_file(str(credentials_file), scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_file_id_by_name_in_folder(
    *,
    credentials_file: Path,
    folder_id: str,
    filename: str,
) -> Optional[str]:
    service = _build_service(credentials_file)
    safe_name = filename.replace("'", "\\'")
    q = (
        "trashed = false "
        f"and '{folder_id}' in parents "
        f"and name = '{safe_name}'"
    )
    resp = (
        service.files()
        .list(q=q, fields="files(id,name)", pageSize=10, supportsAllDrives=True, includeItemsFromAllDrives=True)
        .execute()
    )
    files = resp.get("files") or []
    if not files:
        return None
    return str(files[0].get("id") or "") or None


def upload_or_update_json(
    *,
    credentials_file: Path,
    folder_id: str,
    local_file: Path,
    remote_filename: str,
) -> str:
    if not local_file.exists() or local_file.stat().st_size <= 0:
        raise RuntimeError(f"Local file does not exist or is empty: {local_file}")

    service = _build_service(credentials_file)
    media = MediaFileUpload(str(local_file), mimetype="application/json", resumable=False)

    file_id = find_file_id_by_name_in_folder(
        credentials_file=credentials_file,
        folder_id=folder_id,
        filename=remote_filename,
    )

    if file_id:
        updated = (
            service.files()
            .update(
                fileId=file_id,
                media_body=media,
                body={"name": remote_filename},
                fields="id,name,size",
                supportsAllDrives=True,
            )
            .execute()
        )
        return str(updated.get("id") or file_id)

    created = (
        service.files()
        .create(
            body={"name": remote_filename, "parents": [folder_id]},
            media_body=media,
            fields="id,name,size",
            supportsAllDrives=True,
        )
        .execute()
    )
    created_id = str(created.get("id") or "")
    if not created_id:
        raise RuntimeError("Google Drive upload failed: empty file id")
    return created_id
