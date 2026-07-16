from html.parser import HTMLParser
from pathlib import Path


PANEL_PATH = Path(__file__).resolve().parents[1] / "static" / "admin_panel.html"


class _AdminPanelParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids: set[str] = set()
        self.menu_pages: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"])
        if tag == "a" and values.get("data-page"):
            self.menu_pages.add(values["data-page"])


def _parse_panel() -> _AdminPanelParser:
    parser = _AdminPanelParser()
    parser.feed(PANEL_PATH.read_text(encoding="utf-8"))
    return parser


def test_every_sidebar_destination_has_a_page_container():
    panel = _parse_panel()
    missing = {
        page for page in panel.menu_pages if f"page-{page}" not in panel.ids
    }
    assert not missing


def test_token_and_billing_controls_exist():
    panel = _parse_panel()
    required_ids = {
        "page-tokens",
        "token-name",
        "token-created",
        "token-full",
        "token-list",
        "page-billing",
        "bill-cards",
        "bill-table",
    }
    assert required_ids <= panel.ids


def test_second_brain_tabs_and_dialogs_exist():
    panel = _parse_panel()
    required_ids = {
        "cognition-tab-overview",
        "cognition-tab-projects",
        "cognition-tab-sources",
        "cognition-project-dialog",
        "cognition-source-dialog",
        "cognition-insight-dialog",
        "cognition-insight-dialogue",
        "cognition-insight-correction",
        "cognition-evidence-count",
        "cognition-evidence-sources",
    }
    assert required_ids <= panel.ids


def test_linkable_meeting_detail_controls_exist():
    panel = _parse_panel()
    required_ids = {
        "meeting-detail-dialog",
        "meeting-detail-link",
        "meeting-detail-audio",
        "meeting-detail-transcript",
        "meeting-detail-summary",
        "meeting-detail-insights",
    }
    assert required_ids <= panel.ids
