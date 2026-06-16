from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from powerlit.settings import Settings

logger = logging.getLogger(__name__)

# If modifying these scopes, delete the file google-token.json.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


class GoogleDriveService:
    """Service for uploading files to Google Drive using OAuth 2.0."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._service = None

    def _get_credentials(self) -> Credentials | None:
        creds = None
        token_path = self.settings.google_token_path
        client_secret_path = self.settings.google_client_secret_path

        if not client_secret_path or not client_secret_path.exists():
            logger.error(f"Google client secret not found at {client_secret_path}")
            return None

        if token_path and token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        # If there are no (valid) credentials available, let the user log in.
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(client_secret_path), SCOPES
                )
                # Note: In a headless/remote environment, this will provide a URL
                # and expect the user to paste the code.
                creds = flow.run_local_server(port=0)
            
            # Save the credentials for the next run
            if token_path:
                token_path.parent.mkdir(parents=True, exist_ok=True)
                with open(token_path, "w") as token:
                    token.write(creds.to_json())

        return creds

    @property
    def service(self):
        if self._service is None:
            creds = self._get_credentials()
            if creds:
                self._service = build("drive", "v3", credentials=creds)
        return self._service

    def upload_file(self, file_path: Path, folder_id: str | None = None) -> str | None:
        """Upload a file to Google Drive and return its ID."""
        if not self.service:
            return None

        file_metadata = {
            "name": file_path.name,
        }
        if folder_id or self.settings.google_drive_folder_id:
            target_folder = folder_id or self.settings.google_drive_folder_id
            file_metadata["parents"] = [target_folder]

        media = MediaFileUpload(
            str(file_path),
            mimetype="text/markdown" if file_path.suffix == ".md" else "application/octet-stream",
            resumable=True
        )
        
        try:
            file = self.service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id"
            ).execute()
            logger.info(f"Uploaded file {file_path.name} to Drive, ID: {file.get('id')}")
            return file.get("id")
        except Exception as e:
            logger.error(f"Failed to upload {file_path.name} to Drive: {e}")
            return None

    def upload_parsed_markdown(self, doi: str) -> str | None:
        """Upload the parsed markdown for a specific DOI."""
        from powerlit.services.index import IndexStore
        store = IndexStore(self.settings)
        records = store.load_paper_records(limit=1, doi=doi, unresolved_only=False)
        if not records:
            return None
            
        record = records[0]
        if not record.parsed_md_path:
            logger.warning(f"No parsed markdown path found for DOI {doi}")
            return None
            
        md_path = Path(record.parsed_md_path)
        if not md_path.exists():
            # Try to build path if absolute path is not stored (Repo-relative fallback)
            md_path = self.settings.md_dir / md_path.name
            
        if not md_path.exists():
             logger.error(f"Markdown file not found: {md_path}")
             return None
             
        return self.upload_file(md_path)
