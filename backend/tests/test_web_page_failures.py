"""验证 PageReader 在页面失败、元数据和长行导航上的可观察行为。"""

from __future__ import annotations

import json

from src.tools.types import ToolResult
from src.tools.web_pages import PageReader


def test_failed_fetch_preserves_attempted_url_and_failure_metadata() -> None:
    def fetch(_url: str) -> ToolResult:
        return ToolResult(
            "web_open",
            False,
            "网页请求失败：HTTP 503",
            error_code="http_error",
            metadata={
                "url": "https://origin.example/final",
                "status_code": 503,
                "stage": "response",
                "proxy_used": True,
            },
        )

    result = PageReader({}, fetch, 1_000).execute(
        "open", {"ref_id": "https://origin.example/start"}
    )

    assert result == {
        "type": "open",
        "ref_id": "https://origin.example/start",
        "ok": False,
        "opened": False,
        "url": "https://origin.example/final",
        "status_code": 503,
        "stage": "response",
        "proxy_used": True,
        "error_code": "http_error",
        "content": "网页请求失败：HTTP 503",
    }
    assert "verified" not in result


def test_successful_document_exposes_published_at_without_claiming_verification() -> None:
    published_at = "2026-09-02T08:00:00Z"

    def fetch(url: str) -> ToolResult:
        return ToolResult(
            "web_open",
            True,
            json.dumps({"content": "story"}),
            metadata={
                "document": {
                    "text": "story",
                    "url": url,
                    "title": "Story",
                    "published_at": published_at,
                    "links": [],
                }
            },
        )

    result = PageReader({}, fetch, 1_000).execute(
        "open", {"ref_id": "https://example.com/story"}
    )

    assert result["ok"] is True
    assert result["opened"] is True
    assert result["published_at"] == published_at
    assert "verified" not in result


def test_non_text_fetch_without_document_is_not_cached_as_page_content() -> None:
    result = PageReader(
        {},
        lambda url: ToolResult(
            "web_open",
            True,
            "已读取非文本响应；不返回正文。",
            metadata={"url": url, "content_type": "application/pdf"},
        ),
        1_000,
    ).execute("open", {"ref_id": "https://example.com/file.pdf"})

    assert result["ok"] is False
    assert result["opened"] is False
    assert result["url"] == "https://example.com/file.pdf"
    assert result["error_code"] == "unsupported_content_type"


def test_plain_text_tool_result_remains_a_supported_legacy_fetch_shape() -> None:
    result = PageReader(
        {},
        lambda _url: ToolResult("web_open", True, "alpha\nbeta"),
        1_000,
    ).execute("open", {"ref_id": "https://example.com/source.txt"})

    assert result["ok"] is True
    assert result["opened"] is True
    assert result["content"] == "L0: alpha\nL1: beta"


def test_excerpt_keeps_line_numbers_for_leading_blank_lines() -> None:
    pages = {
        "view1": {
            "text": "\n\nfirst\nsecond",
            "url": "https://example.com/raw.txt",
            "links": [],
        }
    }

    result = PageReader(pages, lambda _url: None, 1_000).execute(
        "open", {"ref_id": "view1"}
    )

    assert result["content"] == "L0: \nL1: \nL2: first\nL3: second"
    assert result["lineno"] == 0
    assert result["total_lines"] == 4


def test_find_centers_long_line_on_match_and_returns_reopenable_offset() -> None:
    prefix = "x" * 5_000
    pages = {
        "view1": {
            "text": f"heading\n{prefix}NEEDLE suffix\nending",
            "url": "https://example.com/long.txt",
            "links": [],
        }
    }
    reader = PageReader(pages, lambda _url: None, 120)

    found = reader.execute("find", {"ref_id": "view1", "pattern": "needle"})

    expected_offset = len("heading\n") + len(prefix)
    assert found["ok"] is True
    assert found["matches"] == [{"lineno": 1, "offset": expected_offset}]
    assert "NEEDLE" in found["content"]
    assert len(found["content"]) <= 120

    reopened = reader.execute(
        "open", {"ref_id": "view1", "offset": found["matches"][0]["offset"], "max_chars": 120}
    )
    assert reopened["lineno"] == 1
    assert reopened["offset"] == expected_offset
    assert "NEEDLE suffix" in reopened["content"]
