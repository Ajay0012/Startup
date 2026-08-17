from __future__ import annotations

import sys

from pangu.cli import parse_args


def test_run_command_is_available(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sys, "argv", ["pangu", "run"])
    args = parse_args()
    assert args.command == "run"
    assert args.text is None
