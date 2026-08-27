from __future__ import annotations

from urllib.parse import unquote_plus

import pytest

from pangu.browser import BrowserElement, BrowserRuntime, BrowserSnapshot, BrowserState
from pangu.media import MediaIntelligenceRuntime, MediaPlaybackState, MediaRequest, MediaSource


class GenericChannelBrowserAdapter:
    def __init__(
        self, channel_name: str, handle: str, latest_title: str, specific_title: str
    ) -> None:
        self.channel_name = channel_name
        self.handle = handle
        self.latest_title = latest_title
        self.specific_title = specific_title
        self.url = ""
        self.started = False
        self.playing = False

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
            if self.channel_name.casefold() in query or self.handle.casefold() in query:
                return BrowserSnapshot(
                    self.url,
                    "YouTube search",
                    "channel results",
                    (
                        BrowserElement(
                            "exact-channel",
                            "link",
                            self.channel_name,
                            href=f"https://www.youtube.com/@{self.handle}",
                        ),
                        BrowserElement(
                            "fan-channel",
                            "link",
                            f"{self.channel_name} Fans",
                            href=f"https://www.youtube.com/@{self.handle}Fans",
                        ),
                        BrowserElement(
                            "reupload",
                            "link",
                            f"{self.channel_name} latest reupload",
                            href="https://www.youtube.com/watch?v=reupload999",
                        ),
                    ),
                    BrowserState.VERIFIED,
                )

        if f"youtube.com/@{self.handle}/videos" in self.url:
            return BrowserSnapshot(
                self.url,
                f"{self.channel_name} - Videos",
                "channel videos",
                (
                    BrowserElement("latest", "button", "Latest"),
                    BrowserElement(
                        "newest",
                        "link",
                        self.latest_title,
                        href="https://www.youtube.com/watch?v=genericlatest123",
                    ),
                    BrowserElement(
                        "older",
                        "link",
                        "Older upload",
                        href="https://www.youtube.com/watch?v=genericolder456",
                    ),
                ),
                BrowserState.VERIFIED,
            )

        if f"youtube.com/@{self.handle}/search?query=" in self.url:
            return BrowserSnapshot(
                self.url,
                f"{self.channel_name} - Search",
                "channel search",
                (
                    BrowserElement(
                        "specific",
                        "link",
                        self.specific_title,
                        href="https://www.youtube.com/watch?v=genericspecific123",
                    ),
                    BrowserElement(
                        "other",
                        "link",
                        "Unrelated video",
                        href="https://www.youtube.com/watch?v=other456",
                    ),
                ),
                BrowserState.VERIFIED,
            )

        return BrowserSnapshot(
            self.url,
            "Video page",
            "video page",
            (BrowserElement("play", "button", "Pause" if self.playing else "Play"),),
            BrowserState.VERIFIED,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "handle", "latest_title"),
    (
        ("MrBeast", "MrBeast", "I Built The World's Largest Challenge"),
        ("Veritasium", "veritasium", "The Physics You Were Never Taught"),
        ("Marques Brownlee", "mkbhd", "The New Phone Review"),
    ),
)
async def test_latest_video_works_for_arbitrary_youtube_channels(
    channel: str,
    handle: str,
    latest_title: str,
) -> None:
    browser = BrowserRuntime(
        GenericChannelBrowserAdapter(channel, handle, latest_title, "Deep Dive")
    )
    await browser.start()
    try:
        media = MediaIntelligenceRuntime(browser)
        result = await media.play(
            MediaRequest(f"latest video from {channel}", source=MediaSource.YOUTUBE)
        )
        assert result.state == MediaPlaybackState.VERIFIED_PLAYING
        assert result.candidate is not None
        assert result.candidate.channel_name == channel
        assert result.candidate.channel_url == f"https://www.youtube.com/@{handle}"
        assert result.candidate.title == latest_title
        assert "genericlatest123" in result.candidate.url
        assert "reupload999" not in result.candidate.url
        assert result.evidence["channel_verified"] is True
        assert result.evidence["selection_mode"] == "channel_latest"
    finally:
        await browser.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "handle", "video_query", "specific_title"),
    (
        ("MrBeast", "MrBeast", "largest challenge", "World's Largest Challenge Explained"),
        ("Veritasium", "veritasium", "electricity", "The Big Misconception About Electricity"),
        ("Marques Brownlee", "mkbhd", "phone review", "The Ultimate Phone Review"),
    ),
)
async def test_specific_video_is_constrained_to_any_requested_channel(
    channel: str,
    handle: str,
    video_query: str,
    specific_title: str,
) -> None:
    browser = BrowserRuntime(
        GenericChannelBrowserAdapter(channel, handle, "Newest Upload", specific_title)
    )
    await browser.start()
    try:
        media = MediaIntelligenceRuntime(browser)
        result = await media.play(
            MediaRequest(f"{video_query} from {channel}", source=MediaSource.YOUTUBE)
        )
        assert result.state == MediaPlaybackState.VERIFIED_PLAYING
        assert result.candidate is not None
        assert result.candidate.channel_name == channel
        assert result.candidate.title == specific_title
        assert "genericspecific123" in result.candidate.url
        assert result.evidence["channel_verified"] is True
        assert result.evidence["selection_mode"] == "channel_specific"
    finally:
        await browser.stop()


def test_channel_scope_parser_is_not_tied_to_known_channel_names() -> None:
    latest = MediaIntelligenceRuntime._youtube_scoped(
        "latest video from Completely New Creator 2040"
    )
    assert latest is not None
    assert latest.channel == "Completely New Creator 2040"
    assert latest.latest is True

    specific = MediaIntelligenceRuntime._youtube_scoped(
        "space documentary from Future Science Channel"
    )
    assert specific is not None
    assert specific.channel == "Future Science Channel"
    assert specific.video_query == "space documentary"
    assert specific.latest is False
