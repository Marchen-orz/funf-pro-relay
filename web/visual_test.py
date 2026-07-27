import os
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".test-runtime" / "visual"
BASE_URL = os.environ.get("FUNF_VISUAL_TEST_URL", "http://127.0.0.1:43871")


def assert_public_feedback(page, width: int, height: int, screenshot: Path) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.goto(BASE_URL, wait_until="networkidle")
    page.locator("#loginView").wait_for(state="visible")
    page.locator("#guestFeedbackButton").click()
    dialog = page.locator("#guestFeedbackDialog")
    dialog.wait_for(state="visible")
    assert dialog.locator("#guestReporterName").is_visible()
    assert dialog.locator("#guestFeedbackCategory").is_visible()
    assert dialog.locator("#guestFeedbackDescription").is_visible()
    dimensions = page.evaluate(
        "() => ({viewport: innerWidth, document: document.documentElement.scrollWidth})"
    )
    assert dimensions["document"] <= dimensions["viewport"], dimensions
    bounds = dialog.bounding_box()
    assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    page.screenshot(path=str(screenshot), full_page=True)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        assert_public_feedback(page, 1440, 900, OUTPUT / "feedback-desktop.png")
        page.locator("#guestFeedbackDialog").evaluate("dialog => dialog.close()")
        assert_public_feedback(page, 390, 844, OUTPUT / "feedback-mobile.png")
        browser.close()
    print("WEB_VISUAL_OK")


if __name__ == "__main__":
    main()
