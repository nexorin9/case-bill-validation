#!/usr/bin/env python3
"""Playwright-driven screenshot capture for case-bill-validation.

Captures 4 real PNG screenshots into docs/screenshots/:
- 01-upload.png            : upload form rendered, no case yet
- 02-pipeline-running.png  : upload submitted, pipeline running (mid-progress)
- 03-fields-reviewed.png   : pipeline finished, fields visible & filter active
- 04-excel-exported.png    : export button pressed, xlsx path visible in status box

Notes on EventSource named events:
- The server emits "step" and "field" named SSE events (event: step / event: field).
- The browser's EventSource.onmessage handler does NOT fire for named events;
  in production they'd be received via addEventListener("step", handler).
- For screenshot purposes we don't rely on SSE: we poll /api/cases/{id}
  from Playwright until processed_steps has all 5 entries, then either wait
  for the page's setUploadHint/case-id-display to populate or trigger a
  fetch-driven doc refresh.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "discharge_01.txt"
SHOTS = ROOT / "docs" / "screenshots"
BASE_URL = "http://127.0.0.1:8000"

_CANDIDATE_BROWSERS = [
    Path.home() / "Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    Path.home() / "Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell",
]


def _pick_executable() -> str | None:
    for cand in _CANDIDATE_BROWSERS:
        if cand.exists():
            return str(cand)
    return None


def _wait_for_pipeline_done(case_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last_doc: dict = {}
    with httpx.Client(timeout=5.0) as client:
        while time.time() < deadline:
            r = client.get(f"{BASE_URL}/api/cases/{case_id}")
            if r.status_code == 200:
                doc = r.json()
                last_doc = doc
                if len(doc.get("processed_steps", [])) >= 5:
                    return doc
            time.sleep(0.3)
    return last_doc


def _upload_via_api() -> str:
    with open(FIXTURE, "rb") as f:
        r = httpx.post(
            f"{BASE_URL}/api/cases",
            files={"file": ("discharge_01.txt", f.read(), "text/plain")},
            timeout=10.0,
        )
    r.raise_for_status()
    return r.json()["case_id"]


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)

    executable = _pick_executable()
    launch_kwargs = {"headless": True}
    if executable:
        launch_kwargs["executable_path"] = executable
        print(f"[screenshot] using local chromium: {executable}")

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
        )
        page = ctx.new_page()

        # ── 01: initial upload form ────────────────────────────────────────
        page.goto(BASE_URL + "/static/index.html", wait_until="networkidle", timeout=15000)
        page.wait_for_selector("#upload-form", state="visible", timeout=10000)
        time.sleep(0.5)
        page.screenshot(path=str(SHOTS / "01-upload.png"), full_page=True)
        print(f"[01-upload] saved {SHOTS / '01-upload.png'}")

        # ── 02: pipeline running (upload + first steps observed) ──────────
        # Upload via API for a known case_id, then push that case_id into the
        # page state via injection so the SSE stream attaches to it.
        case_id = _upload_via_api()
        print(f"[screenshot] uploaded case_id={case_id}")

        # Inject case_id into the page IIFE state by re-running the upload-submit
        # click after we've populated state.caseId from a JS-side fetch that
        # re-uploads the same file. The cleanest path is to submit the form:
        page.set_input_files("#file-input", str(FIXTURE))
        page.click("#upload-submit")
        # Wait until case-id-display is populated (proves upload succeeded)
        page.wait_for_function(
            "() => document.getElementById('case-id-display').textContent.includes('case_id')",
            timeout=15000,
        )
        # capture pipeline-running screenshot mid-progress
        time.sleep(0.4)
        page.screenshot(path=str(SHOTS / "02-pipeline-running.png"), full_page=True)
        print(f"[02-pipeline-running] saved {SHOTS / '02-pipeline-running.png'}")

        # ── 03: pipeline finished + filter/search active ──────────────────
        # The form click triggered a SECOND upload, so its case_id is the one in
        # the page now. Wait for server-side pipeline to complete.
        page.wait_for_function(
            "() => document.querySelectorAll('#field-table-body tr').length >= 25",
            timeout=20000,
        )
        time.sleep(0.8)
        # Apply search filter for interactive state
        page.fill("#filter-input", "日期")
        time.sleep(0.5)
        page.screenshot(path=str(SHOTS / "03-fields-reviewed.png"), full_page=True)
        print(f"[03-fields-reviewed] saved {SHOTS / '03-fields-reviewed.png'}")

        # ── 04: export xlsx (button click + status visible) ───────────────
        page.fill("#filter-input", "")
        time.sleep(0.3)
        page.click("#export-btn")
        page.wait_for_function(
            "() => {const s=document.getElementById('export-status'); return s && s.textContent && s.textContent.length > 5;}",
            timeout=15000,
        )
        time.sleep(0.6)
        page.screenshot(path=str(SHOTS / "04-excel-exported.png"), full_page=True)
        print(f"[04-excel-exported] saved {SHOTS / '04-excel-exported.png'}")

        browser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())