from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

from .browser import BrowserActionKind, BrowserActionRequest, BrowserRuntime, BrowserState


class MediaSource(StrEnum):
    AUTO = "auto"
    YOUTUBE = "youtube"
    VIMEO = "vimeo"
    DAILYMOTION = "dailymotion"
    ARCHIVE = "archive"
    DIRECT = "direct"


class MediaPlaybackState(StrEnum):
    VERIFIED_PLAYING = "VERIFIED_PLAYING"
    OPENED_UNVERIFIED = "OPENED_UNVERIFIED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    DENIED = "DENIED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class MediaCandidate:
    source: MediaSource
    title: str
    url: str
    score: float
    evidence: tuple[str, ...] = ()
    channel_name: str | None = None
    channel_url: str | None = None


@dataclass(frozen=True)
class MediaRequest:
    query: str
    source: MediaSource = MediaSource.AUTO
    direct_url: str | None = None
    prefer_official: bool = True
    minimum_score: float = 0.48
    ambiguity_margin: float = 0.06


@dataclass(frozen=True)
class MediaPlaybackResult:
    state: MediaPlaybackState
    message: str
    candidate: MediaCandidate | None = None
    alternatives: tuple[MediaCandidate, ...] = ()
    evidence: dict[str, object] = field(default_factory=dict)
    normalized_error: str | None = None


@dataclass(frozen=True)
class _SourceSpec:
    source: MediaSource
    search_url: str
    hosts: tuple[str, ...]
    media_path_pattern: re.Pattern[str]


@dataclass(frozen=True)
class _YouTubeScopedRequest:
    channel: str
    video_query: str | None
    latest: bool


_SOURCES = (
    _SourceSpec(
        MediaSource.YOUTUBE,
        "https://www.youtube.com/results?search_query={query}",
        ("youtube.com", "www.youtube.com", "youtu.be"),
        re.compile(r"(?:/watch\?|youtu\.be/|/shorts/)", re.IGNORECASE),
    ),
    _SourceSpec(
        MediaSource.VIMEO,
        "https://vimeo.com/search?q={query}",
        ("vimeo.com", "www.vimeo.com"),
        re.compile(r"^/\d+(?:$|[/?#])"),
    ),
    _SourceSpec(
        MediaSource.DAILYMOTION,
        "https://www.dailymotion.com/search/{query}/videos",
        ("dailymotion.com", "www.dailymotion.com", "dai.ly"),
        re.compile(r"(?:/video/|dai\.ly/)", re.IGNORECASE),
    ),
    _SourceSpec(
        MediaSource.ARCHIVE,
        "https://archive.org/search?query={query}%20AND%20mediatype%3Amovies",
        ("archive.org", "www.archive.org"),
        re.compile(r"^/details/", re.IGNORECASE),
    ),
)


class MediaIntelligenceRuntime:
    """Search, channel-resolve, rank, open and verify media in PANGU's browser.

    YouTube channel-scoped requests resolve the channel first and only select videos
    observed inside that channel's own Videos/search surface. This prevents a global
    result, re-upload, reaction, or similarly named channel from satisfying a hard
    channel constraint.
    """

    _official_terms = ("official", "vevo", "trailer", "topic")
    _noise_terms = ("playlist", "mix -", "compilation", "reaction", "review")
    _play_names = ("play", "play video", "play (k)", "resume", "resume video")
    _channel_paths = ("/@", "/channel/", "/c/", "/user/")

    def __init__(self, browser: BrowserRuntime) -> None:
        self.browser = browser

    @staticmethod
    def source(value: str | MediaSource | None) -> MediaSource:
        if isinstance(value, MediaSource):
            return value
        key = (value or "auto").strip().casefold().replace(" ", "")
        aliases = {
            "auto": MediaSource.AUTO,
            "youtube": MediaSource.YOUTUBE,
            "yt": MediaSource.YOUTUBE,
            "vimeo": MediaSource.VIMEO,
            "dailymotion": MediaSource.DAILYMOTION,
            "archive": MediaSource.ARCHIVE,
            "archive.org": MediaSource.ARCHIVE,
            "direct": MediaSource.DIRECT,
        }
        return aliases.get(key, MediaSource.AUTO)

    @staticmethod
    def _tokens(value: str) -> tuple[str, ...]:
        return tuple(
            token
            for token in re.findall(r"[a-z0-9]+", value.casefold())
            if len(token) > 1 and token not in {"the", "a", "an", "video", "song", "play"}
        )

    @classmethod
    def _score(
        cls,
        query: str,
        title: str,
        url: str,
        source: MediaSource,
        prefer_official: bool,
    ) -> tuple[float, tuple[str, ...]]:
        query_norm = " ".join(query.casefold().split())
        title_norm = " ".join(title.casefold().split())
        query_tokens = set(cls._tokens(query_norm))
        title_tokens = set(cls._tokens(title_norm))
        evidence: list[str] = []
        score = 0.0
        if query_norm and query_norm in title_norm:
            score += 0.42
            evidence.append("exact phrase in title")
        if query_tokens:
            coverage = len(query_tokens & title_tokens) / len(query_tokens)
            score += 0.43 * coverage
            if coverage:
                evidence.append(f"token coverage {coverage:.2f}")
        if source == MediaSource.YOUTUBE and "/watch" in url:
            score += 0.08
            evidence.append("standard video result")
        elif source in {MediaSource.VIMEO, MediaSource.DAILYMOTION, MediaSource.ARCHIVE}:
            score += 0.05
        if prefer_official and any(term in title_norm for term in cls._official_terms):
            score += 0.07
            evidence.append("official/publisher signal")
        if any(term in title_norm for term in cls._noise_terms):
            score -= 0.08
            evidence.append("secondary-content penalty")
        return max(0.0, min(score, 1.0)), tuple(evidence)

    @staticmethod
    def _spec(source: MediaSource) -> _SourceSpec | None:
        return next((item for item in _SOURCES if item.source == source), None)

    @classmethod
    def _matching_media_url(cls, href: str, spec: _SourceSpec) -> bool:
        try:
            parsed = urlparse(href)
        except ValueError:
            return False
        host = (parsed.hostname or "").casefold()
        if host not in spec.hosts:
            return False
        candidate = (
            href
            if spec.source == MediaSource.DAILYMOTION
            else parsed.path + ("?" + parsed.query if parsed.query else "")
        )
        return spec.media_path_pattern.search(candidate) is not None

    @staticmethod
    def _with_autoplay(candidate: MediaCandidate) -> str:
        if candidate.source != MediaSource.YOUTUBE:
            return candidate.url
        parsed = urlparse(candidate.url)
        if "youtube.com" not in (parsed.hostname or "").casefold() or parsed.path != "/watch":
            return candidate.url
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["autoplay"] = "1"
        return urlunparse(parsed._replace(query=urlencode(query)))

    @staticmethod
    def _youtube_scoped(query: str) -> _YouTubeScopedRequest | None:
        clean = " ".join(query.strip().split())
        latest = re.fullmatch(
            r"(?:the\s+)?latest\s+(?:video|upload)(?:\s+on\s+youtube)?\s+(?:from|of|by)\s+(.+?)(?:\s+channel)?",
            clean,
            re.IGNORECASE,
        )
        if latest:
            return _YouTubeScopedRequest(latest.group(1).strip(), None, True)
        latest_alt = re.fullmatch(
            r"(?:the\s+)?latest\s+(?:video|upload)\s+(?:from|of|by)\s+(.+?)(?:\s+on\s+youtube)?",
            clean,
            re.IGNORECASE,
        )
        if latest_alt:
            return _YouTubeScopedRequest(latest_alt.group(1).strip(), None, True)
        specific = re.fullmatch(
            r"(.+?)\s+(?:video\s+)?(?:from|of|by)\s+(.+?)(?:\s+channel)?(?:\s+on\s+youtube)?",
            clean,
            re.IGNORECASE,
        )
        if specific:
            return _YouTubeScopedRequest(specific.group(2).strip(), specific.group(1).strip(), False)
        return None

    @classmethod
    def _channel_score(cls, requested: str, observed: str) -> float:
        wanted = " ".join(requested.casefold().split())
        actual = " ".join(observed.casefold().split())
        if not actual:
            return 0.0
        if actual == wanted:
            return 1.0
        wanted_tokens = set(cls._tokens(wanted))
        actual_tokens = set(cls._tokens(actual))
        if not wanted_tokens:
            return 0.0
        coverage = len(wanted_tokens & actual_tokens) / len(wanted_tokens)
        phrase = 0.2 if wanted in actual or actual in wanted else 0.0
        return min(0.98, coverage * 0.78 + phrase)

    @classmethod
    def _channel_url(cls, href: str) -> str | None:
        try:
            parsed = urlparse(href)
        except ValueError:
            return None
        if (parsed.hostname or "").casefold() not in {"youtube.com", "www.youtube.com"}:
            return None
        if not any(parsed.path.startswith(prefix) for prefix in cls._channel_paths):
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return None
        if parts[0].startswith("@"):
            base_path = "/" + parts[0]
        elif len(parts) >= 2 and parts[0] in {"channel", "c", "user"}:
            base_path = f"/{parts[0]}/{parts[1]}"
        else:
            return None
        return urlunparse(("https", "www.youtube.com", base_path, "", "", ""))

    async def _resolve_youtube_channel(self, channel: str) -> tuple[str, str] | None:
        search_url = "https://www.youtube.com/results?search_query=" + quote_plus(channel)
        opened = await self.browser.execute(
            BrowserActionRequest(BrowserActionKind.NAVIGATE, url=search_url)
        )
        if opened.state in {BrowserState.DENIED, BrowserState.FAILED, BrowserState.UNAVAILABLE}:
            return None
        snapshot = await self.browser.snapshot()
        ranked: list[tuple[float, str, str]] = []
        for element in snapshot.elements:
            if not element.visible or not element.href:
                continue
            channel_url = self._channel_url(element.href)
            if channel_url is None:
                continue
            name = " ".join(element.name.split())
            score = self._channel_score(channel, name)
            if score >= 0.70:
                ranked.append((score, name, channel_url))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.04 and ranked[0][0] < 0.95:
            return None
        _, name, url = ranked[0]
        return name, url

    @staticmethod
    def _regular_youtube_candidates(snapshot_elements: tuple[object, ...]) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw in snapshot_elements:
            href = getattr(raw, "href", None)
            visible = bool(getattr(raw, "visible", False))
            name = " ".join(str(getattr(raw, "name", "")).split())
            if not href or not visible or not name:
                continue
            parsed = urlparse(str(href))
            if (parsed.hostname or "").casefold() not in {"youtube.com", "www.youtube.com"}:
                continue
            if parsed.path != "/watch" or not dict(parse_qsl(parsed.query)).get("v"):
                continue
            canonical = urlunparse(("https", "www.youtube.com", "/watch", "", urlencode({"v": dict(parse_qsl(parsed.query))["v"]}), ""))
            if canonical in seen:
                continue
            lower = name.casefold()
            if lower.startswith("live ") or " #shorts" in lower:
                continue
            seen.add(canonical)
            candidates.append((name[:512], canonical))
        return candidates

    async def _search_youtube_channel(
        self,
        scoped: _YouTubeScopedRequest,
        prefer_official: bool,
    ) -> MediaPlaybackResult:
        resolved = await self._resolve_youtube_channel(scoped.channel)
        if resolved is None:
            return MediaPlaybackResult(
                MediaPlaybackState.NOT_FOUND,
                f"I couldn't confidently resolve the YouTube channel '{scoped.channel}'.",
                normalized_error="YOUTUBE_CHANNEL_NOT_FOUND",
            )
        channel_name, channel_url = resolved
        if scoped.latest:
            target = channel_url + "/videos"
            opened = await self.browser.execute(
                BrowserActionRequest(BrowserActionKind.NAVIGATE, url=target)
            )
            if opened.state in {BrowserState.DENIED, BrowserState.FAILED, BrowserState.UNAVAILABLE}:
                return MediaPlaybackResult(
                    MediaPlaybackState.FAILED,
                    f"I found {channel_name}, but couldn't open its Videos feed.",
                    normalized_error="YOUTUBE_CHANNEL_VIDEOS_UNAVAILABLE",
                )
            snapshot = await self.browser.snapshot()
            latest_controls = [
                item
                for item in snapshot.elements
                if item.visible
                and item.enabled
                and item.role.casefold() == "button"
                and item.name.casefold().strip() == "latest"
            ]
            if len(latest_controls) == 1:
                await self.browser.execute(
                    BrowserActionRequest(
                        BrowserActionKind.CLICK,
                        target_name=latest_controls[0].name,
                        target_role=latest_controls[0].role,
                    )
                )
                snapshot = await self.browser.snapshot()
            videos = self._regular_youtube_candidates(snapshot.elements)
            if not videos:
                return MediaPlaybackResult(
                    MediaPlaybackState.NOT_FOUND,
                    f"I found {channel_name}, but couldn't identify a regular uploaded video.",
                    normalized_error="YOUTUBE_CHANNEL_HAS_NO_VIDEO_MATCH",
                )
            title, url = videos[0]
            candidate = MediaCandidate(
                MediaSource.YOUTUBE,
                title,
                url,
                1.0,
                ("channel identity resolved", "latest regular video from channel Videos feed"),
                channel_name,
                channel_url,
            )
            return MediaPlaybackResult(
                MediaPlaybackState.OPENED_UNVERIFIED,
                f"Selected the latest regular video from {channel_name}: {title}.",
                candidate,
                evidence={"channel_verified": True, "selection_mode": "channel_latest"},
            )

        video_query = scoped.video_query or ""
        target = channel_url + "/search?query=" + quote_plus(video_query)
        opened = await self.browser.execute(
            BrowserActionRequest(BrowserActionKind.NAVIGATE, url=target)
        )
        if opened.state in {BrowserState.DENIED, BrowserState.FAILED, BrowserState.UNAVAILABLE}:
            return MediaPlaybackResult(
                MediaPlaybackState.FAILED,
                f"I found {channel_name}, but couldn't search inside the channel.",
                normalized_error="YOUTUBE_CHANNEL_SEARCH_UNAVAILABLE",
            )
        snapshot = await self.browser.snapshot()
        ranked: list[MediaCandidate] = []
        for title, url in self._regular_youtube_candidates(snapshot.elements):
            score, evidence = self._score(
                video_query, title, url, MediaSource.YOUTUBE, prefer_official
            )
            ranked.append(
                MediaCandidate(
                    MediaSource.YOUTUBE,
                    title,
                    url,
                    min(1.0, score + 0.12),
                    (*evidence, "channel identity resolved", "result observed inside channel"),
                    channel_name,
                    channel_url,
                )
            )
        ranked.sort(key=lambda item: item.score, reverse=True)
        if not ranked or ranked[0].score < 0.55:
            return MediaPlaybackResult(
                MediaPlaybackState.NOT_FOUND,
                f"I found {channel_name}, but not a confident match for '{video_query}' on that channel.",
                alternatives=tuple(ranked[:3]),
                normalized_error="YOUTUBE_CHANNEL_VIDEO_NOT_FOUND",
            )
        best = ranked[0]
        if len(ranked) > 1 and best.score - ranked[1].score < 0.05 and best.score < 0.90:
            return MediaPlaybackResult(
                MediaPlaybackState.AMBIGUOUS,
                f"I found multiple close matches inside {channel_name}; I won't guess.",
                alternatives=tuple(ranked[:3]),
                normalized_error="YOUTUBE_CHANNEL_VIDEO_AMBIGUOUS",
            )
        return MediaPlaybackResult(
            MediaPlaybackState.OPENED_UNVERIFIED,
            f"Selected {best.title} from {channel_name}.",
            best,
            tuple(ranked[1:4]),
            {"channel_verified": True, "selection_mode": "channel_specific"},
        )

    async def _search_source(
        self, query: str, spec: _SourceSpec, prefer_official: bool
    ) -> tuple[MediaCandidate, ...]:
        search_url = spec.search_url.format(query=quote_plus(query))
        opened = await self.browser.execute(
            BrowserActionRequest(BrowserActionKind.NAVIGATE, url=search_url)
        )
        if opened.state in {BrowserState.DENIED, BrowserState.FAILED, BrowserState.UNAVAILABLE}:
            return ()
        snapshot = await self.browser.snapshot()
        if snapshot.verification_state != BrowserState.VERIFIED:
            return ()
        unique: dict[str, MediaCandidate] = {}
        for element in snapshot.elements:
            href = element.href
            if not href or not element.visible or not self._matching_media_url(href, spec):
                continue
            title = " ".join(element.name.split())
            if not title:
                continue
            score, evidence = self._score(query, title, href, spec.source, prefer_official)
            existing = unique.get(href)
            candidate = MediaCandidate(spec.source, title[:512], href, score, evidence)
            if existing is None or candidate.score > existing.score:
                unique[href] = candidate
        return tuple(
            sorted(unique.values(), key=lambda item: (item.score, len(item.title)), reverse=True)[:12]
        )

    async def search(self, request: MediaRequest) -> MediaPlaybackResult:
        query = " ".join(request.query.strip().split())
        direct = request.direct_url or (
            query if query.startswith(("http://", "https://")) else None
        )
        if direct:
            if not self.browser.allowed_url(direct):
                return MediaPlaybackResult(
                    MediaPlaybackState.DENIED,
                    "That media URL is blocked by browser safety policy.",
                    normalized_error="MEDIA_URL_BLOCKED",
                )
            candidate = MediaCandidate(
                MediaSource.DIRECT,
                query or direct,
                direct,
                1.0,
                ("owner supplied direct URL",),
            )
            return MediaPlaybackResult(
                MediaPlaybackState.OPENED_UNVERIFIED, "Direct media target accepted.", candidate
            )
        if not query:
            return MediaPlaybackResult(
                MediaPlaybackState.NOT_FOUND,
                "Tell me what you want to play.",
                normalized_error="EMPTY_MEDIA_QUERY",
            )
        if not self.browser.started:
            return MediaPlaybackResult(
                MediaPlaybackState.UNAVAILABLE,
                "The isolated PANGU browser is not running.",
                normalized_error="BROWSER_NOT_STARTED",
            )

        scoped = self._youtube_scoped(query)
        if scoped is not None and request.source in {MediaSource.AUTO, MediaSource.YOUTUBE}:
            return await self._search_youtube_channel(scoped, request.prefer_official)

        specs = list(_SOURCES) if request.source == MediaSource.AUTO else [self._spec(request.source)]
        candidates: list[MediaCandidate] = []
        for spec in specs:
            if spec is None:
                continue
            found = await self._search_source(query, spec, request.prefer_official)
            candidates.extend(found)
            if request.source == MediaSource.AUTO and found and found[0].score >= 0.86:
                break
        ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
        if not ranked or ranked[0].score < request.minimum_score:
            return MediaPlaybackResult(
                MediaPlaybackState.NOT_FOUND,
                f"I couldn't find a confident media match for '{query}'.",
                alternatives=tuple(ranked[:3]),
                normalized_error="MEDIA_NOT_FOUND",
            )
        best = ranked[0]
        if (
            len(ranked) > 1
            and best.score - ranked[1].score < request.ambiguity_margin
            and best.score < 0.86
        ):
            return MediaPlaybackResult(
                MediaPlaybackState.AMBIGUOUS,
                "I found multiple close matches; I won't guess which one you meant.",
                alternatives=tuple(ranked[:3]),
                normalized_error="MEDIA_AMBIGUOUS",
            )
        return MediaPlaybackResult(
            MediaPlaybackState.OPENED_UNVERIFIED,
            f"Selected {best.title} from {best.source.value}.",
            best,
            tuple(ranked[1:4]),
        )

    async def play(self, request: MediaRequest) -> MediaPlaybackResult:
        selected = await self.search(request)
        candidate = selected.candidate
        if candidate is None:
            return selected
        target_url = self._with_autoplay(candidate)
        opened = await self.browser.execute(
            BrowserActionRequest(BrowserActionKind.NAVIGATE, url=target_url)
        )
        if opened.state in {BrowserState.DENIED, BrowserState.UNAVAILABLE}:
            return MediaPlaybackResult(
                MediaPlaybackState.DENIED,
                "The selected media target was blocked.",
                candidate,
                normalized_error=opened.normalized_error,
            )
        if opened.state == BrowserState.FAILED:
            return MediaPlaybackResult(
                MediaPlaybackState.FAILED,
                "I found the media but couldn't open it.",
                candidate,
                normalized_error="MEDIA_OPEN_FAILED",
            )

        snapshot = await self.browser.snapshot()
        if any(
            item.visible
            and item.enabled
            and item.role.casefold() == "button"
            and "pause" in item.name.casefold()
            for item in snapshot.elements
        ):
            return MediaPlaybackResult(
                MediaPlaybackState.VERIFIED_PLAYING,
                f"Playing {candidate.title} on {candidate.source.value}.",
                candidate,
                evidence={
                    **selected.evidence,
                    "playback_signal": "pause control visible",
                    "url": snapshot.url,
                },
            )

        play_targets = [
            item
            for item in snapshot.elements
            if item.visible
            and item.enabled
            and item.role.casefold() == "button"
            and (
                item.name.casefold().strip() in self._play_names
                or item.name.casefold().strip().startswith("play ")
            )
            and "playlist" not in item.name.casefold()
        ]
        if play_targets:
            target = play_targets[0]
            await self.browser.execute(
                BrowserActionRequest(
                    BrowserActionKind.CLICK,
                    target_name=target.name,
                    target_role=target.role,
                )
            )
            after = await self.browser.snapshot()
            if any(
                item.visible
                and item.role.casefold() == "button"
                and "pause" in item.name.casefold()
                for item in after.elements
            ):
                return MediaPlaybackResult(
                    MediaPlaybackState.VERIFIED_PLAYING,
                    f"Playing {candidate.title} on {candidate.source.value}.",
                    candidate,
                    evidence={
                        **selected.evidence,
                        "playback_signal": "play control changed to pause",
                        "url": after.url,
                    },
                )

        final = await self.browser.snapshot()
        return MediaPlaybackResult(
            MediaPlaybackState.OPENED_UNVERIFIED,
            f"I opened {candidate.title}, but the site did not expose a reliable playback-state signal.",
            candidate,
            evidence={
                **selected.evidence,
                "url": final.url,
                "page_title": final.title,
                "autoplay_requested": candidate.source == MediaSource.YOUTUBE,
            },
            normalized_error="PLAYBACK_STATE_UNVERIFIED",
        )
