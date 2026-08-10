from __future__ import annotations

from urllib.parse import unquote_plus

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
        if role == "button" and name == "Latest":
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
            query = unquote_plus(self.url.split("search_query=", 1)[1]).casefold()
            if "vj siddhu vlogs" in query:
                return BrowserSnapshot(
                    self.url,
                    "YouTube search",
                    "channel search results",
                    (
                        BrowserElement(
                            "channel-1",
                            "link",
                            "VJ Siddhu Vlogs",
                            href="https://www.youtube.com/@VjSiddhuVlogs",
                        ),
                        BrowserElement(
                            "channel-2",
                            "link",
                            "VJ Siddhu Vlogs Fans",
                            href="https://www.youtube.com/@VjSiddhuVlogsFans",
                        ),
                        BrowserElement(
                            "reupload",
                            "link",
                            "VJ Siddhu Vlogs latest video reupload",
                            href="https://www.youtube.com/watch?v=reupload999",
                        ),
                    ),
                    BrowserState.VERIFIED,
                )
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

        if "youtube.com/@VjSiddhuVlogs/videos" in self.url:
            return BrowserSnapshot(
                self.url,
                "VJ Siddhu Vlogs - Videos",
                "channel videos",
                (
                    BrowserElement("latest", "button", "Latest"),
                    BrowserElement(
                        "short",
                        "link",
                        "Funny moment #Shorts",
                        href="https://www.youtube.com/shorts/short123",
                    ),
                    BrowserElement(
                        "newest",
                        "link",
                        "VJ Siddhu Vlogs Newest Adventure",
                        href="https://www.youtube.com/watch?v=newest123",
                    ),
                    BrowserElement(
                        "older",
                        "link",
                        "VJ Siddhu Vlogs Older Trip",
                        href="https://www.youtube.com/watch?v=older456",
                    ),
                ),
                BrowserState.VERIFIED,
            )

        if "youtube.com/@VjSiddhuVlogs/search?query=" in self.url:
            query = unquote_plus(self.url.split("query=", 1)[1]).casefold()
            if "birthday surprise" in query:
                return BrowserSnapshot(
                    self.url,
                    "VJ Siddhu Vlogs - Search",
                    "channel search",
                    (
                        BrowserElement(
                            "specific",
                            "link",
                            "Birthday Surprise for the Team | VJ Siddhu Vlogs",
                            href="https://www.youtube.com/watch?v=birthday123",
                        ),
                        BrowserElement(
                            "other",
                            "link",
                            "Birthday Shopping Vlog | VJ Siddhu Vlogs",
                            href="https://www.youtube.com/watch?v=shopping456",
                        ),
                    ),
                    BrowserState.VERIFIED,
                )

        controls = (
            BrowserElement("play", "button", "Pause" if self.playing else "Play"),
        )
        return BrowserSnapshot(
            self.url,
            "Video page",
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
async def test_latest_video_is_selected_only_after_exact_channel_resolution() -> None:
    browser = BrowserRuntime(FakeMediaBrowserAdapter())
    await browser.start()
    try:
        media = MediaIntelligenceRuntime(browser)
        result = await media.play(
            MediaRequest("latest video from VJ Siddhu Vlogs", source=MediaSource.YOUTUBE)
        )
        assert result.state == MediaPlaybackState.VERIFIED_PLAYING
        assert result.candidate is not None
        assert result.candidate.channel_name == "VJ Siddhu Vlogs"
        assert result.candidate.channel_url == "https://www.youtube.com/@VjSiddhuVlogs"
        assert result.candidate.title == "VJ Siddhu Vlogs Newest Adventure"
        assert "newest123" in result.candidate.url
        assert "reupload999" not in result.candidate.url
        assert result.evidence["channel_verified"] is True
        assert result.evidence["selection_mode"] == "channel_latest"
    finally:
        await browser.stop()


@pytest.mark.asyncio
async def test_specific_video_is_searched_inside_exact_channel() -> None:
    browser = BrowserRuntime(FakeMediaBrowserAdapter())
    await browser.start()
    try:
        media = MediaIntelligenceRuntime(browser)
        result = await media.play(
            MediaRequest("birthday surprise from VJ Siddhu Vlogs", source=MediaSource.YOUTUBE)
        )
        assert result.state == MediaPlaybackState.VERIFIED_PLAYING
        assert result.candidate is not None
        assert result.candidate.channel_name == "VJ Siddhu Vlogs"
        assert result.candidate.title == "Birthday Surprise for the Team | VJ Siddhu Vlogs"
        assert "birthday123" in result.candidate.url
        assert result.evidence["channel_verified"] is True
        assert result.evidence["selection_mode"] == "channel_specific"
    finally:
        await browser.stop()


@pytest.mark.asyncio
async def test_channel_constraint_does_not_fall_back_to_global_reupload() -> None:
    browser = BrowserRuntime(FakeMediaBrowserAdapter())
    await browser.start()
    try:
        media = MediaIntelligenceRuntime(browser)
        result = await media.search(
            MediaRequest("latest video from Missing Exact Channel", source=MediaSource.YOUTUBE)
        )
        assert result.state == MediaPlaybackState.NOT_FOUND
        assert result.normalized_error == "YOUTUBE_CHANNEL_NOT_FOUND"
        assert result.candidate is None
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

    provider_from = language.normalize("play Interstellar from YouTube")
    assert provider_from.entities == {"query": "Interstellar", "source": "YouTube"}

    tanglish = language.normalize("youtube la vikram trailer play pannu")
    assert tanglish.intent_name == "play_media"
    assert tanglish.entities["query"] == "vikram trailer"
    assert tanglish.entities["source"] == "youtube"
    assert tanglish.detected_language == "ta-en"


def test_media_language_understands_latest_and_specific_channel_requests() -> None:
    language = LanguageRuntime()
    latest = language.normalize("play latest video from VJ Siddhu Vlogs")
    assert latest.intent_name == "play_media"
    assert latest.entities == {
        "query": "latest video from VJ Siddhu Vlogs",
        "source": "youtube",
    }

    latest_explicit = language.normalize("play latest video from VJ Siddhu Vlogs on YouTube")
    assert latest_explicit.entities == {
        "query": "latest video from VJ Siddhu Vlogs",
        "source": "youtube",
    }

    specific = language.normalize("play birthday surprise from VJ Siddhu Vlogs")
    assert specific.entities == {
        "query": "birthday surprise from VJ Siddhu Vlogs",
        "source": "youtube",
    }

    tanglish = language.normalize("vj siddhu vlogs oda latest video play pannu")
    assert tanglish.entities["query"] == "latest video from vj siddhu vlogs"
    assert tanglish.detected_language == "ta-en"


def test_generic_play_request_uses_auto_source_selection() -> None:
    intent = LanguageRuntime().normalize("play nasa moon landing documentary")
    assert intent.intent_name == "play_media"
    assert intent.entities["source"] == "auto"
    assert intent.entities["query"] == "nasa moon landing documentary"
