"""Integração com Google Drive via OAuth 2.0 (Client ID)."""

import os.path
import traceback

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
DRIVE_FOLDER_ID = "1v-bDAijlnOwzUHehndsGfFMDhqmHCB5o"
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


def _escapar_query_drive(valor):
    return str(valor).replace("\\", "\\\\").replace("'", "\\'")


def _get_or_create_tenant_folder(tenant_slug, service=None):
    """Retorna o ID da pasta do condomínio dentro de DRIVE_FOLDER_ID."""
    slug = (tenant_slug or "").strip()
    if not slug:
        return DRIVE_FOLDER_ID

    if service is None:
        service = _get_drive_service()

    slug_q = _escapar_query_drive(slug)
    query = (
        f"name = '{slug_q}' and "
        "mimeType = 'application/vnd.google-apps.folder' and "
        f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
    )
    resposta = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id, name)", pageSize=1)
        .execute()
    )
    existentes = resposta.get("files") or []
    if existentes and existentes[0].get("id"):
        return existentes[0]["id"]

    pasta = (
        service.files()
        .create(
            body={
                "name": slug,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [DRIVE_FOLDER_ID],
            },
            fields="id",
        )
        .execute()
    )
    return pasta.get("id") or DRIVE_FOLDER_ID


def upload_to_drive(file_obj, filename=None, tenant_slug=None):
    """
    Envia um FileStorage (ou stream) para a pasta do condomínio no Drive.

    Retorna {"id", "webViewLink"} em sucesso, ou None se a API falhar
    (para não interromper o cadastro do morador).
    """
    try:
        if file_obj is None:
            return None

        nome = filename or getattr(file_obj, "filename", None) or "documento"
        stream = file_obj.stream if hasattr(file_obj, "stream") else file_obj
        if hasattr(stream, "seek"):
            stream.seek(0)

        service = _get_drive_service()
        pasta_id = _get_or_create_tenant_folder(tenant_slug, service=service)

        mimetype = getattr(file_obj, "content_type", None) or "application/octet-stream"
        media = MediaIoBaseUpload(stream, mimetype=mimetype, resumable=True)
        file_metadata = {"name": nome, "parents": [pasta_id]}

        arquivo = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id,webViewLink")
            .execute()
        )

        file_id = arquivo.get("id")
        if not file_id:
            return None

        user_permission = {"type": "anyone", "role": "reader"}
        service.permissions().create(fileId=file_id, body=user_permission).execute()

        return {"id": file_id, "webViewLink": arquivo.get("webViewLink")}
    except Exception:
        traceback.print_exc()
        return None


def upload_file_stream(file_obj, filename=None, tenant_slug=None):
    """Upload de FileStorage do Flask na pasta do condomínio (`tenant_slug`)."""
    return upload_to_drive(file_obj, filename=filename, tenant_slug=tenant_slug)


def delete_from_drive(file_id):
    """Remove um ficheiro do Drive. Falhas (ex.: já inexistente) não bloqueiam."""
    if not file_id:
        return False
    try:
        service = _get_drive_service()
        service.files().delete(fileId=file_id).execute()
        return True
    except Exception:
        traceback.print_exc()
        return False
