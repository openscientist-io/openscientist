"""The navigator credits C-BRAIN under the OpenScientist title, its name on a white chip with a red AI."""

from html.parser import HTMLParser

import pytest
from nicegui import ui
from nicegui.testing import user_simulation

from openscientist.webapp_components.components.navigation import render_navigator


class _Styled(HTMLParser):
    """Each text run of an HTML fragment with the inline styles around it, outermost first."""

    def __init__(self) -> None:
        super().__init__()
        self.styles: list[str] = []
        self.runs: list[tuple[str, tuple[str, ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.styles.append(dict(attrs).get("style") or "")

    def handle_endtag(self, tag: str) -> None:
        self.styles.pop()

    def handle_data(self, data: str) -> None:
        self.runs.append((data, tuple(self.styles)))


@pytest.mark.asyncio
async def test_the_navigator_credits_c_brain_on_a_white_chip_with_its_red_ai() -> None:
    async with user_simulation(root=render_navigator) as browser:
        await browser.open("/")
        await browser.should_see("OpenScientist")
        fragments = [element.content for element in browser.find(ui.html).elements]
    credits = [fragment for fragment in fragments if "C-BR" in fragment]
    assert len(credits) == 1
    parsed = _Styled()
    parsed.feed(credits[0])
    assert "".join(text for text, _ in parsed.runs) == "by C-BRAIN"
    (ai_styles,) = [styles for text, styles in parsed.runs if text == "AI"]
    assert "color:#aa0010" in ai_styles[-1]
    assert all("color:#aa0010" not in styles[-1] for text, styles in parsed.runs if text != "AI")
    chip = ai_styles[-2]
    assert "background:#ffffff" in chip
    assert "color:#000000" in chip
    (by_styles,) = [styles for text, styles in parsed.runs if text == "by"]
    assert "color:#ffffff" in by_styles[0]
    assert all("background" not in style for style in by_styles)
