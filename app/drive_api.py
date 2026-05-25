"""Integração com Google Drive via OAuth 2.0 (Client ID)."""

import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_FOLDER_ID = "1__3z-vm9LB8_Cfv4j8E97fPtT-ec9Ezt"
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
CLIENT_SECRET_PATH = os.path.join(BASE_DIR, "client_secret.json")
TOKEN_PATH = os.path.join(BASE_DIR, "token.json")


def obter_credenciais():
    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_PATH,
                SCOPES,
            )
            creds = flow.run_local_server(port=8080)

        with open(TOKEN_PATH, "w", encoding="utf-8") as token_file:
            token_file.write(creds.to_json())

    return creds


def _get_drive_service():
    creds = obter_credenciais()
    return build("drive", "v3", credentials=creds)


def upload_to_drive(file_stream, filename):
    service = _get_drive_service()

    stream = file_stream.stream if hasattr(file_stream, "stream") else file_stream
    if hasattr(stream, "seek"):
        stream.seek(0)

    media = MediaIoBaseUpload(
        stream,
        mimetype=getattr(file_stream, "content_type", "application/octet-stream"),
        resumable=True,
    )
    file_metadata = {"name": filename, "parents": [DRIVE_FOLDER_ID]}

    arquivo = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id,webViewLink")
        .execute()
    )

    file_id = arquivo.get("id")
    user_permission = {"type": "anyone", "role": "reader"}
    service.permissions().create(fileId=file_id, body=user_permission).execute()

    return {"id": file_id, "webViewLink": arquivo.get("webViewLink")}
