from __future__ import annotations

import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver
from powerlit.services.incoming_processor import IncomingPDFProcessor
from powerlit.services.rag_index import RAGIndexService
from powerlit.services.drive_upload import GoogleDriveService
from powerlit.settings import Settings

logger = logging.getLogger(__name__)


class PDFIncomingHandler(FileSystemEventHandler):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.processor = IncomingPDFProcessor(settings)
        self.indexer = RAGIndexService(settings)
        self.drive = GoogleDriveService(settings)

    def on_created(self, event):
        if event.is_directory:
            return
        if Path(event.src_path).suffix.lower() == ".pdf":
            self._process_new_pdf(Path(event.src_path))

    def _process_new_pdf(self, pdf_path: Path):
        logger.info(f"New PDF detected: {pdf_path.name}")
        # Wait a bit to ensure file is fully written/closed by OS
        time.sleep(2)
        
        try:
            # 1. Process PDF (DOI lookup, transcription, analysis)
            result = self.processor.process_file(
                pdf_path,
                parse=True,
                analyze=True,
                force_overwrite=True
            )
            
            # 2. Update Vector Index
            if result.parsed_json_path:
                logger.info(f"Updating RAG index for {result.doi}...")
                self.indexer.incremental_index(result.parsed_json_path)
            
            # 3. Upload to Google Drive (Cloud sync is now a manual/topic-based step)
            # if result.doi:
            #     logger.info(f"Syncing {result.doi} to Google Drive...")
            #     self.drive.upload_parsed_markdown(result.doi)
            
            logger.info(f"Successfully processed and indexed: {pdf_path.name}")
            
        except Exception as e:
            logger.error(f"Failed to process {pdf_path.name}: {e}")


class IncomingWatcherService:
    """Service for background monitoring of incoming PDFs."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.observer = None

    def start(self):
        watch_path = self.settings.incoming_pdf_dir
        if not watch_path.exists():
            watch_path.mkdir(parents=True, exist_ok=True)

        logger.info(f"Starting watcher on {watch_path}")
        handler = PDFIncomingHandler(self.settings)
        self.observer = PollingObserver()
        self.observer.schedule(handler, str(watch_path), recursive=False)
        self.observer.start()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            logger.info("Watcher stopped.")
