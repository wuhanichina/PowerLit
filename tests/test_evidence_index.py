from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from powerlit.services.evidence_index import EvidenceIndexService
from powerlit.settings import Settings


def test_evidence_index_build_search_filters_and_schema(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    write_parsed_json(
        settings.json_root / "ieee_tsg" / "zbus.json",
        {
            "doi": "10.1109/tsg.2024.000001",
            "title": "Analytical Z-Bus Model for Voltage Unbalance",
            "source_title": "IEEE Transactions on Smart Grid",
            "year": 2024,
            "content": (
                "# Analytical Z-Bus Model for Voltage Unbalance\n\n"
                "## Methodology\n\n"
                "The proposed Z-Bus formulation characterizes voltage unbalance factor "
                "in three-phase distribution networks.\n\n"
                "## Case Study\n\n"
                "The method is tested on unbalanced feeders."
            ),
        },
    )
    write_parsed_json(
        settings.json_root / "ieee_tpwrs" / "lindistflow.json",
        {
            "doi": "10.1109/tpwrs.2023.000002",
            "title": "Fast LinDistFlow Screening",
            "source_title": "IEEE Transactions on Power Systems",
            "year": 2023,
            "content": "## Model\n\nLinDistFlow screening for distribution planning.",
        },
    )
    write_parsed_json(
        settings.json_root / "ieee_tsg" / "skip-analysis-analysis.json",
        {"content": "Z-Bus voltage unbalance should be skipped."},
    )
    write_parsed_json(
        settings.rag_output_dir / "skip-rag.json",
        {"content": "Z-Bus voltage unbalance should also be skipped."},
    )
    write_parsed_json(settings.json_root / "empty.json", {"title": "empty"})
    write_minimal_papers_db(
        settings.db_path,
        doi="10.1109/tsg.2024.000001",
        local_pdf_path="/tmp/zbus.pdf",
        analysis_json_path="/tmp/zbus-analysis.json",
    )

    service = EvidenceIndexService(settings)
    summary = service.build(force=True)

    assert summary.documents == 2
    assert summary.chunks >= 2
    assert summary.skipped == 1

    payload = service.search("Z-Bus voltage unbalance", top=5)
    assert payload["available"] is True
    assert payload["candidate_source"] == "powerlit_evidence_fts"
    assert payload["elapsed_ms"] >= 0
    assert payload["count"] >= 1

    first = payload["results"][0]
    assert first["doi"] == "10.1109/tsg.2024.000001"
    assert first["title"] == "Analytical Z-Bus Model for Voltage Unbalance"
    assert first["source_title"] == "IEEE Transactions on Smart Grid"
    assert first["year"] == 2024
    assert first["section"] == "Methodology"
    assert first["parsed_json_path"].endswith("zbus.json")
    assert first["local_pdf_path"] == "/tmp/zbus.pdf"
    assert first["analysis_json_path"] == "/tmp/zbus-analysis.json"
    assert "Z-Bus" in first["snippet"]
    assert first["chunk_id"]

    assert service.search("Z-Bus", venue_folders=["ieee_tpwrs"])["count"] == 0
    assert service.search("Z-Bus", venue_folders=["ieee_tsg"])["count"] >= 1
    assert service.search("Z-Bus", year_from=2024, year_to=2024)["count"] >= 1
    assert service.search("Z-Bus", year_to=2023)["count"] == 0
    assert service.search("Z-Bus", doi="10.1109/tsg.2024.000001")["count"] >= 1
    assert service.search("Z-Bus", section="Method")["count"] >= 1


def test_rebuild_same_doi_does_not_duplicate_chunks(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    write_parsed_json(
        settings.json_root / "ieee_tsg" / "paper.json",
        {
            "doi": "10.1109/tsg.2024.000003",
            "title": "Duplicate Safe Indexing",
            "source_title": "IEEE Transactions on Smart Grid",
            "year": 2024,
            "content": "## Method\n\nZ-Bus evidence chunk.\n\nAnother Z-Bus evidence chunk.",
        },
    )
    service = EvidenceIndexService(settings)

    service.build(force=True)
    first_status = service.status()
    service.build()
    second_status = service.status()

    assert first_status["documents"] == second_status["documents"] == 1
    assert first_status["chunks"] == second_status["chunks"]
    assert service.search("Z-Bus")["count"] >= 1


def test_search_without_index_returns_unavailable_payload(tmp_path: Path) -> None:
    settings = make_test_settings(tmp_path)
    service = EvidenceIndexService(settings)

    payload = service.search("Z-Bus")
    status = service.status()

    assert payload["available"] is False
    assert payload["count"] == 0
    assert payload["results"] == []
    assert "build-evidence-index" in payload["message"]
    assert status["available"] is False


def test_evidence_search_performance_smoke_10k_chunks(tmp_path: Path) -> None:
    if os.environ.get("POWERLIT_PERF_TEST") != "1":
        pytest.skip("Set POWERLIT_PERF_TEST=1 to run the 10k chunk smoke test.")

    settings = make_test_settings(tmp_path)
    blocks = [
        f"Z-Bus voltage unbalance synthetic chunk {index}. " + ("x " * 900)
        for index in range(10_000)
    ]
    write_parsed_json(
        settings.json_root / "ieee_tsg" / "perf.json",
        {
            "doi": "10.1109/tsg.2024.perf",
            "title": "Performance Smoke",
            "source_title": "IEEE Transactions on Smart Grid",
            "year": 2024,
            "content": "\n\n".join(blocks),
        },
    )
    service = EvidenceIndexService(settings)
    service.build(force=True)

    payload = service.search("Z-Bus voltage unbalance", top=20)

    assert payload["count"] == 20
    assert payload["elapsed_ms"] < 50


def make_test_settings(tmp_path: Path) -> Settings:
    literature = tmp_path / "literature"
    return Settings(
        literature_root=literature,
        reference_dir=literature / "reference",
        md_dir=literature / "md",
        metadata_dir=literature / "metadata",
        index_dir=literature / "index",
        vector_index_dir=literature / "index" / "vector_index",
        json_root=literature / "json",
        index_root=literature / "index" / "evidence",
        reports_dir=literature / "reports",
        weekly_reports_dir=literature / "reports" / "weekly",
        monthly_reports_dir=literature / "reports" / "monthly",
        output_dir=literature / "metadata",
        download_list_dir=literature / "metadata" / "download_list",
        rag_output_dir=literature / "json" / "rag",
        cas_journal_list_path=tmp_path / "cas_journal_whitelist.xlsx",
        db_path=literature / "metadata" / "papers.db",
        incoming_pdf_dir=tmp_path / "incoming_pdf",
        parsed_output_dir=literature / "json",
        analysis_output_dir=literature / "json",
        debug_output_dir=tmp_path / "debug",
        embedding_device="cpu",
    )


def write_parsed_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_minimal_papers_db(
    db_path: Path,
    *,
    doi: str,
    local_pdf_path: str,
    analysis_json_path: str,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE papers (
                doi TEXT,
                local_pdf_path TEXT,
                analysis_json_path TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO papers (doi, local_pdf_path, analysis_json_path)
            VALUES (?, ?, ?)
            """,
            (doi, local_pdf_path, analysis_json_path),
        )
