from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

from pangu.personalized_wake import acoustic_features, build_profile_payload, speech_regions
from pangu.settings import resolve_application_root
from pangu.speaker_identity import SherpaSpeakerEmbeddingProvider

TARGET_SAMPLE_RATE = 16000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enroll the owner's natural 'Hey Pangu' pronunciation and voice locally."
    )
    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="PortAudio input device index. Omit to use the Windows default microphone.",
    )
    parser.add_argument("--samples", type=int, default=8, help="successful enrollment utterances")
    parser.add_argument("--seconds", type=float, default=3.5, help="recording duration per utterance")
    parser.add_argument(
        "--speaker-threshold",
        type=float,
        default=0.78,
        help="maximum learned owner speaker threshold",
    )
    args = parser.parse_args()
    if not 4 <= args.samples <= 20:
        parser.error("--samples must be between 4 and 20")
    if not 2.0 <= args.seconds <= 8.0:
        parser.error("--seconds must be between 2 and 8")
    if not 0.65 <= args.speaker_threshold <= 0.92:
        parser.error("--speaker-threshold must be between 0.65 and 0.92")
    return args


def expand_region(start: int, end: int, total: int, sample_rate: int) -> tuple[int, int]:
    minimum = round(1.25 * sample_rate)
    if end - start >= minimum:
        return start, end
    center = (start + end) // 2
    left = max(0, center - minimum // 2)
    right = min(total, left + minimum)
    left = max(0, right - minimum)
    return left, right


def resolve_capture_rate(device: int | None) -> tuple[int, str]:
    """Prefer 16 kHz, but fall back to the microphone's native rate when required."""
    try:
        sd.check_input_settings(
            device=device,
            channels=1,
            dtype="float32",
            samplerate=TARGET_SAMPLE_RATE,
        )
        info = sd.query_devices(device, "input") if device is not None else sd.query_devices(kind="input")
        return TARGET_SAMPLE_RATE, str(info.get("name", "default microphone"))
    except (sd.PortAudioError, ValueError):
        try:
            info = sd.query_devices(device, "input") if device is not None else sd.query_devices(kind="input")
        except (sd.PortAudioError, ValueError) as error:
            raise SystemExit(f"INPUT_DEVICE_UNAVAILABLE: {error}") from error

        native = round(float(info.get("default_samplerate", 0.0)))
        if native <= 0:
            raise SystemExit("INPUT_DEVICE_UNAVAILABLE: microphone has no usable default sample rate")
        try:
            sd.check_input_settings(
                device=device,
                channels=1,
                dtype="float32",
                samplerate=native,
            )
        except (sd.PortAudioError, ValueError) as error:
            raise SystemExit(
                f"INPUT_DEVICE_UNAVAILABLE: microphone cannot open at 16000 Hz or native {native} Hz: {error}"
            ) from error
        return native, str(info.get("name", "microphone"))


def resample_to_target(waveform: np.ndarray, source_rate: int) -> np.ndarray:
    if source_rate == TARGET_SAMPLE_RATE or waveform.size == 0:
        return waveform.astype(np.float32, copy=False)
    target_size = max(1, round(waveform.size * TARGET_SAMPLE_RATE / source_rate))
    source_positions = np.arange(waveform.size, dtype=np.float64)
    target_positions = np.linspace(0.0, waveform.size - 1, target_size, dtype=np.float64)
    return np.interp(target_positions, source_positions, waveform).astype(np.float32)


def main() -> int:
    args = parse_args()
    root = resolve_application_root()
    speaker_model = (
        root
        / "models"
        / "voice"
        / "speaker"
        / "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
    )
    if not speaker_model.is_file():
        raise SystemExit(
            "SPEAKER_MODEL_UNAVAILABLE: run .\\scripts\\install-speaker-model.ps1 first"
        )

    capture_rate, device_name = resolve_capture_rate(args.device)
    provider = SherpaSpeakerEmbeddingProvider(speaker_model, minimum_seconds=1.0)
    templates: list[np.ndarray] = []
    speaker_embeddings: list[np.ndarray] = []

    print()
    print("PANGU OWNER WAKE ENROLLMENT")
    print("Your recordings are processed locally and raw audio is not saved.")
    print("Say 'Hey Pangu' naturally, using your normal slang/pronunciation.")
    print("Use a normal speaking volume; do not shout.")
    print(f"Microphone: {device_name}")
    print(f"Capture sample rate: {capture_rate} Hz")
    if capture_rate != TARGET_SAMPLE_RATE:
        print(f"Audio will be resampled locally to {TARGET_SAMPLE_RATE} Hz for PANGU models.")
    print()

    attempt = 0
    max_attempts = args.samples * 3
    while len(templates) < args.samples and attempt < max_attempts:
        attempt += 1
        number = len(templates) + 1
        print(f"Sample {number}/{args.samples}: recording starts in 1 second...")
        time.sleep(1.0)
        try:
            recording = sd.rec(
                round(args.seconds * capture_rate),
                samplerate=capture_rate,
                channels=1,
                dtype="float32",
                device=args.device,
            )
            sd.wait()
        except sd.PortAudioError as error:
            raise SystemExit(
                f"INPUT_STREAM_FAILED: device={args.device!r}, sample_rate={capture_rate}: {error}"
            ) from error

        captured = np.asarray(recording[:, 0], dtype=np.float32)
        waveform = resample_to_target(captured, capture_rate)
        peak = float(np.max(np.abs(waveform))) if waveform.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(waveform)))) if waveform.size else 0.0
        regions = speech_regions(waveform, TARGET_SAMPLE_RATE)
        if not regions:
            print(f"  rejected: no clear phrase region (rms={rms:.4f}, peak={peak:.4f})")
            continue
        start, end = max(regions, key=lambda item: item[1] - item[0])
        start, end = expand_region(start, end, waveform.size, TARGET_SAMPLE_RATE)
        phrase = waveform[start:end]
        try:
            features = acoustic_features(phrase, TARGET_SAMPLE_RATE)
            embedding = np.asarray(
                provider.extract(
                    tuple(float(value) for value in phrase),
                    TARGET_SAMPLE_RATE,
                ),
                dtype=np.float32,
            )
        except (RuntimeError, ValueError) as error:
            print(f"  rejected: {error}")
            continue
        if features.shape[0] < 45:
            print("  rejected: phrase was too short; say the full 'Hey Pangu'")
            continue
        templates.append(features)
        speaker_embeddings.append(embedding)
        print(f"  accepted (rms={rms:.4f}, peak={peak:.4f})")

    if len(templates) < args.samples:
        raise SystemExit(
            f"ENROLLMENT_INCOMPLETE: accepted {len(templates)}/{args.samples} samples"
        )

    payload = build_profile_payload(
        tuple(templates),
        tuple(speaker_embeddings),
        speaker_similarity_threshold=args.speaker_threshold,
    )
    identity_dir = root / "runtime-data" / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)
    target = identity_dir / "owner_wake_profile.json"
    temp = identity_dir / "owner_wake_profile.json.tmp"
    temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temp.replace(target)

    print()
    print("ENROLLED")
    print(f"profile={target}")
    print(f"samples={payload['enrollment_count']}")
    print(f"keyword_threshold={payload['keyword_threshold']:.4f}")
    print(f"speaker_similarity_threshold={payload['speaker_similarity_threshold']:.4f}")
    print("raw_audio_persisted=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
