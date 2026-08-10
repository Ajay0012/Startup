from __future__ import annotations

import pytest

from pangu.browser import BrowserElement, BrowserRuntime, BrowserSnapshot, BrowserState
from pangu.language import LanguageRuntime
from pangu.media import MediaIntelligenceRuntime, MediaPlaybackState, MediaRequest, MediaSource


class FakeMediaBrowserAdapter:
    def __init__(self) -> None:
        self.url = ""
        self.playing = False
        self.started = False

    async def start(self) -> bool:
        self.started = True
        return True

    async def stop(self) -> None:
        self.started = False

    async def navigate(self, url: str) -> bool:
        self.url = url
        self.playing = False
        return True

    async def click(self, role: str, name: str) -> bool:
        if role == "button" and name == "Play":
            self.playing = True
            return True
        return False

    async def fill(self, role: str, name: str, text: str) -> bool:
        return False

    async def back(self) -> bool:
        return True

    async def forward(self) -> bool:
        return True

    async def snapshot(self) -> BrowserSnapshot:
        if "/results?search_query=" in self.url:
            return BrowserSnapshot(
                self.url,
                "YouTube search",
                "search results",
                (
                    BrowserElement(
                        "1",
                        "link",
                        "Interstellar Official Trailer 4K",
                        href="https://www.youtube.com/watch?v=official123",
                    ),
                    BrowserElement(
                        "2",
                        "link",
                        "Interstellar trailer reaction and review",
                        href="https://www.youtube.com/watch?v=reaction456",
                    ),
                ),
                BrowserState.VERIFIED,
            )
        controls = (
            BrowserElement("play", "button", "Pause" if self.playing else "Play"),
        )
        return BrowserSnapshot(
            self.url,
            "Interstellar Official Trailer 4K",
            "video page",
            controls,
            BrowserState.VERIFIED,
        )


@pytest.mark.asyncio
async def test_youtube_exact_match_is_selected_and_playback_verified() -> None:
    browser = BrowserRuntime(FakeMediaBrowserAdapter())
    await browser.start()
    try:
        media = MediaIntelligenceRuntime(browser)
        result = await media.play(
            MediaRequest("Interstellar Official Trailer", source=MediaSource.YOUTUBE)
        )
        assert result.state == MediaPlaybackState.VERIFIED_PLAYING
        assert result.candidate is not None
        assert result.candidate.title == "Interstellar Official Trailer 4K"
        assert result.candidate.score > 0.8
        assert result.evidence["playback_signal"] == "play control changed to pause"
    finally:
        await browser.stop()


@pytest.mark.asyncio
async def test_private_direct_media_url_is_denied() -> None:
    browser = BrowserRuntime(FakeMediaBrowserAdapter())
    await browser.start()
    try:
        media = MediaIntelligenceRuntime(browser)
        result = await media.search(
            MediaRequest(
                "http://127.0.0.1/private-video",
                source=MediaSource.DIRECT,
                direct_url="http://127.0.0.1/private-video",
            )
        )
        assert result.state == MediaPlaybackState.DENIED
        assert result.normalized_error == "MEDIA_URL_BLOCKED"
    finally:
        await browser.stop()


def test_media_language_understands_provider_and_tanglish() -> None:
    language = LanguageRuntime()
    youtube = language.normalize("play Interstellar trailer on YouTube")
    assert youtube.intent_name == "play_media"
    assert youtube.entities == {"query": "Interstellar trailer", "source": "YouTube"}

    tanglish = language.normalize("youtube la vikram trailer play pannu")
    assert tanglish.intent_name == "play_media"
    assert tanglish.entities["query"] == "vikram trailer"
    assert tanglish.entities["source"] == "youtube"
    assert tanglish.detected_language == "ta-en"


def test_generic_play_request_uses_auto_source_selection() -> None:
    intent = LanguageRuntime().normalize("play nasa moon landing documentary")
    assert intent.intent_name == "play_media"
    assert intent.entities["source"] == "auto"
    assert intent.entities["query"] == "nasa moon landing documentary"
