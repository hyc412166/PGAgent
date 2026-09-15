# Web Source Research Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement the approved tasks and verify their results. Do not commit unrelated user changes.

**Goal:** Reproduce the observable Codex search/open/find/click source-reading workflow with PGAgent's independent backend.

**Architecture:** Keep the existing web_run dispatch and turn-scoped page state. Use the current safe HTTP boundary for remote content, and add a focused page-reading module for numbered text, links, cached continuation and matches. Search remains independent of model credentials.

**Tech Stack:** Python, httpx, HTMLParser, pytest; no new external service or dependency.

**Spec:** User-approved second route in this conversation: source-derived interfaces and comparable research functionality, not access to Codex's private alpha/search backend.

## Global Constraints

- Preserve all existing uncommitted changes, especially builtins.py find errors and shell output metadata.
- Keep SSRF, redirects, response-size, timeout and approval safeguards.
- Do not change frontend, financial/weather tools, model providers or credentials.
- Reference: openai/codex commit 3abbf9fe2c6b6910e9de61f6a0c5bb468f74b5c8, codex-rs/codex-api/src/search.rs and codex-rs/ext/web-search/src/{tool,schema,output}.rs.
- Use existing aliases where already supported; no new compatibility framework or release gates.

## Task 1: Page research loop

Files: backend/src/tools/{builtins,web_pages,registry}.py; backend/tests/test_web_research.py.

- [ ] Add HTTP-fixture tests proving find reaches beyond the initial output, lineno reads stable cached content, click uses real extracted links, errors and truncation remain visible.
- [ ] Run `python -m pytest backend/tests/test_web_research.py -q` and observe missing behavior.
- [ ] Implement page extraction and cache records containing URL, text, links and acquisition truncation; render only requested excerpts with zero-based L line labels and next_lineno.
- [ ] Expose Codex-derived open/ref_id/lineno, find/ref_id/pattern, click/ref_id/id and response_length schemas. Keep existing offset/max_chars callers working.
- [ ] Verify long lines, HTML preformatted code, direct GitHub blob URLs, invalid refs, redirects and blocked private destinations.

## Task 2: Search correctness and guidance

Files: backend/src/tools/builtins.py; backend/src/agent/engine.py; backend/tests/test_web_tools.py.

- [ ] Reproduce irrelevant Bing results and isolate URL encoding versus upstream behavior without new credentials.
- [ ] If a reproducible fix exists, add a regression test for the outbound request then implement that fix. Preserve errors and source/recency semantics.
- [ ] Add concise guidance to read primary source bodies, follow symbols/links, continue truncated pages and state evidence limits.

## Task 3: Verification and delivery

- [ ] Run new tests plus existing web, tool-runtime and cross-call page-state tests.
- [ ] Exercise real GitHub source search/open/find/continuation with the production registry; report actual search relevance, not merely HTTP status.
- [ ] Request independent code review, resolve material findings, run final tests and `git diff --check`.
- [ ] Deliver changes, source provenance, verification and any remaining external-service limitations; no unsolicited commit or deployment.
