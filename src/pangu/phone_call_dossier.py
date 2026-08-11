from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable


class CallSpeaker(StrEnum):
    PANGU = "pangu"
    COUNTERPARTY = "counterparty"
    OWNER = "owner"
    SYSTEM = "system"


@dataclass(frozen=True)
class CallTranscriptTurn:
    speaker: CallSpeaker
    text: str
    at_monotonic: float = field(default_factory=time.monotonic)
    confidence: float = 1.0
    sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("call transcript text is required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("call transcript confidence must be between 0 and 1")


@dataclass(frozen=True)
class CallCommitment:
    speaker: CallSpeaker
    statement: str
    category: str


@dataclass(frozen=True)
class CallFact:
    kind: str
    value: str
    source_turn: int


@dataclass(frozen=True)
class CallDossier:
    purpose: str
    target: str
    outcome: str
    complete_conversation: tuple[CallTranscriptTurn, ...]
    commitments: tuple[CallCommitment, ...]
    facts: tuple[CallFact, ...]
    owner_confirmations: tuple[str, ...]
    unresolved_items: tuple[str, ...]
    concise_briefing: str
    privacy_note: str


class CallDossierBuilder:
    """Build a complete owner-facing call report without forcing transcript persistence.

    The live transcript can remain memory-only. Sensitive values are preserved for the owner
    in the immediate dossier, while callers may persist only `redacted_transcript()` or the
    structured facts/commitments according to the configured retention policy.
    """

    _phone = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{7,}\d)(?!\d)")
    _email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    _money = re.compile(r"(?:₹|Rs\.?|INR|\$|USD|€|EUR)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", re.I)
    _reference = re.compile(r"\b(?:booking|appointment|reference|confirmation|ref)\s*(?:id|no\.?|number|code)?\s*[:#-]?\s*([A-Z0-9-]{4,})\b", re.I)
    _time = re.compile(r"\b(?:[01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:am|pm)?\b|\b(?:1[0-2]|0?[1-9])\s*(?:am|pm)\b", re.I)
    _date = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|(?:mon|tues|wednes|thurs|fri|satur|sun)day|today|tomorrow)\b", re.I)
    _commitment_terms = (
        "confirmed",
        "booked",
        "scheduled",
        "reserved",
        "will call",
        "will send",
        "will be",
        "agreed",
        "approved",
        "accepted",
    )
    _unresolved_terms = (
        "not sure",
        "call back",
        "pending",
        "unavailable",
        "cannot confirm",
        "to be confirmed",
        "waiting",
    )

    def __init__(self, purpose: str, target: str) -> None:
        self.purpose = purpose.strip()
        self.target = target.strip()
        self._turns: list[CallTranscriptTurn] = []
        self._owner_confirmations: list[str] = []
        self._explicit_facts: list[CallFact] = []

    @property
    def turns(self) -> tuple[CallTranscriptTurn, ...]:
        return tuple(self._turns)

    def record_turn(
        self,
        speaker: CallSpeaker,
        text: str,
        *,
        confidence: float = 1.0,
        sensitive: bool = False,
    ) -> CallTranscriptTurn:
        turn = CallTranscriptTurn(speaker, " ".join(text.split())[:8000], confidence=confidence, sensitive=sensitive)
        self._turns.append(turn)
        return turn

    def record_owner_confirmation(self, summary: str) -> None:
        clean = " ".join(summary.split())[:1000]
        if clean:
            self._owner_confirmations.append(clean)

    def record_fact(self, kind: str, value: str, source_turn: int = -1) -> None:
        clean = " ".join(value.split())[:1000]
        if clean:
            self._explicit_facts.append(CallFact(kind.strip()[:80] or "fact", clean, source_turn))

    @staticmethod
    def _redact(text: str) -> str:
        text = CallDossierBuilder._email.sub("[EMAIL REDACTED]", text)
        return CallDossierBuilder._phone.sub("[PHONE REDACTED]", text)

    def redacted_transcript(self) -> tuple[CallTranscriptTurn, ...]:
        return tuple(
            CallTranscriptTurn(
                turn.speaker,
                "[SENSITIVE CONTENT WITHHELD]" if turn.sensitive else self._redact(turn.text),
                turn.at_monotonic,
                turn.confidence,
                turn.sensitive,
            )
            for turn in self._turns
        )

    def _extract_facts(self) -> tuple[CallFact, ...]:
        facts = list(self._explicit_facts)
        seen = {(item.kind, item.value.casefold()) for item in facts}
        for index, turn in enumerate(self._turns):
            text = turn.text
            for kind, pattern in (
                ("price", self._money),
                ("reference", self._reference),
                ("date", self._date),
                ("time", self._time),
            ):
                for match in pattern.finditer(text):
                    value = match.group(0).strip()
                    key = (kind, value.casefold())
                    if key not in seen:
                        seen.add(key)
                        facts.append(CallFact(kind, value[:300], index))
        return tuple(facts[:128])

    def _commitments(self) -> tuple[CallCommitment, ...]:
        result: list[CallCommitment] = []
        for turn in self._turns:
            lower = turn.text.casefold()
            if any(term in lower for term in self._commitment_terms):
                category = "booking" if any(term in lower for term in ("booked", "scheduled", "reserved", "confirmed")) else "promise"
                result.append(CallCommitment(turn.speaker, turn.text[:1200], category))
        return tuple(result[:64])

    def _unresolved(self) -> tuple[str, ...]:
        items: list[str] = []
        for turn in self._turns:
            lower = turn.text.casefold()
            if any(term in lower for term in self._unresolved_terms):
                items.append(turn.text[:1000])
        return tuple(dict.fromkeys(items))[:32]

    @staticmethod
    def _briefing(outcome: str, facts: Iterable[CallFact], commitments: Iterable[CallCommitment], unresolved: Iterable[str]) -> str:
        facts_list = list(facts)
        commitments_list = list(commitments)
        unresolved_list = list(unresolved)
        pieces = [f"Call outcome: {outcome}."]
        if commitments_list:
            pieces.append(f"{len(commitments_list)} commitment(s) were made.")
        if facts_list:
            highlights = ", ".join(f"{item.kind}={item.value}" for item in facts_list[:6])
            pieces.append(f"Key details: {highlights}.")
        if unresolved_list:
            pieces.append(f"{len(unresolved_list)} item(s) remain unresolved.")
        else:
            pieces.append("No unresolved item was detected.")
        return " ".join(pieces)

    def build(self, *, outcome: str) -> CallDossier:
        facts = self._extract_facts()
        commitments = self._commitments()
        unresolved = self._unresolved()
        return CallDossier(
            self.purpose,
            self.target,
            outcome,
            tuple(self._turns),
            commitments,
            facts,
            tuple(self._owner_confirmations),
            unresolved,
            self._briefing(outcome, facts, commitments, unresolved),
            (
                "Complete transcript is intended for immediate owner review. Raw audio is not retained by this component; "
                "persist only a redacted transcript unless the owner explicitly enabled transcript retention and applicable consent rules are satisfied."
            ),
        )
