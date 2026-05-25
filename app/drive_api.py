"""Módulo de integração com a API do Google Drive via Service Account."""

import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

DRIVE_FOLDER_ID = os.environ.get(
    "DRIVE_FOLDER_ID", "1__3z-vm9LB8_Cfv4j8E97fPtT-ec9Ezt"
)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

CREDENTIALS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "credentials.json"
)

MIME_TYPES = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}


def _get_drive_service():
    credentials = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH, scopes=SCOPES
    )
    return build("drive", "v3", credentials=credentials)


def upload_to_drive(file_stream, filename):
    """Faz upload de arquivo para a pasta do condomínio no Google Drive.

    Args:
        file_stream: Stream do arquivo (ex: request.files['documento'])
        filename: Nome do arquivo a ser salvo no Drive

    Returns:
        dict com 'id' e 'webViewLink' do arquivo criado no Drive
    """
    extensao = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    mime_type = MIME_TYPES.get(extensao, "application/octet-stream")

    service = _get_drive_service()

    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID],
    }

    media = MediaIoBaseUpload(file_stream, mimetype=mime_type, resumable=True)

    arquivo = (
        service.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id, webViewLink",
        )
        .execute()
    )

    return {
        "id": arquivo.get("id"),
        "webViewLink": arquivo.get("webViewLink"),
    }
