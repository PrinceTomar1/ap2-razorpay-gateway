"""Build the pitch video from the slides and the narration.

Deliberately dumb and reproducible. Each slide is held for exactly as long as its
own audio, so re-recording a line changes the timing and nothing else needs
touching.

Audio is the narrator's own recordings in ``voice/``. There is deliberately no
synthetic fallback: a pitch is somebody explaining their own build, and if the
recordings are incomplete the right outcome is a loud failure, not a video that is
half one voice and half another.

Each clip runs through a restoration chain before it reaches the timeline —
high-pass, denoise, de-ess, compression, a presence lift, then two-pass loudness
normalisation. The order matters and is explained at CHAIN below.

    python3 build.py                       # natural pace
    python3 build.py --tempo 1.20          # brisker, to fit a hard time limit
    python3 build.py --fit 300             # pick the tempo that lands under 5:00
    python3 build.py --lufs -16            # broadcast target instead of YouTube
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from narration import SCRIPT

HERE = pathlib.Path(__file__).parent
VOICE_DIR = HERE / "voice"
SLIDE_DIR = HERE / "slides"
OUTPUT = HERE / "ap2-razorpay-pitch.mp4"

PAD = 0.4  # a beat of silence after each slide
AUDIO_EXTS = ("mp3", "m4a", "wav", "aiff", "aac", "flac")

#: YouTube normalises playback to roughly -14 LUFS, so mastering there means the
#: platform leaves the track alone. -16 is the broadcast/podcast convention.
DEFAULT_LUFS = -14.0
TRUE_PEAK = -1.5

#: The restoration chain, in the only order that makes sense:
#:
#:   highpass   kill rumble and desk thump first, so nothing downstream wastes
#:              headroom on energy nobody can hear
#:   afftdn     spectral denoise on the now-clean low end. nf=-25 is gentle; it
#:              lifts hiss without the underwater artefacts a harder setting gives
#:   equalizer  a narrow -4 dB dip at 6.5 kHz — de-essing. Sibilance sits here, and
#:              a notch beats a compressor that pumps the whole track
#:   acompressor 2.5:1 above -20 dB. Quiet words come up, loud ones stop short of
#:              clipping. Gentle enough that the delivery still breathes
#:   equalizer  +3 dB centred at 3.5 kHz — presence. This is the band that decides
#:              whether consonants land, which is what "make the words clear" means
#:   loudnorm   last, always. Anything after it would undo the measurement
CHAIN = (
    "highpass=f=85,"
    "afftdn=nf=-25,"
    "equalizer=f=6500:width_type=q:w=2.5:g=-4,"
    "acompressor=threshold=-20dB:ratio=2.5:attack=8:release=180:makeup=2,"
    "equalizer=f=3500:width_type=q:w=1.2:g=3"
)


def duration(path: pathlib.Path) -> float:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(json.loads(probe)["format"]["duration"])


def recordings() -> list[pathlib.Path] | None:
    """One audio file per slide from voice/, or None if any is missing."""
    found: list[pathlib.Path] = []
    for index in range(len(SCRIPT)):
        matches = [p for ext in AUDIO_EXTS if (p := VOICE_DIR / f"{index:02d}.{ext}").exists()]
        if not matches:
            return None
        found.append(matches[0])
    return found


def measure(audio: pathlib.Path, chain: str, lufs: float) -> dict[str, str]:
    """First loudnorm pass: measure, so the second pass can correct exactly.

    Single-pass loudnorm guesses from a running window and drifts on short clips.
    Two-pass costs one extra decode and lands on the target.
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(audio),
            "-af",
            f"{chain},loudnorm=I={lufs}:TP={TRUE_PEAK}:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    blob = result[result.rindex("{") : result.rindex("}") + 1]
    stats: dict[str, str] = json.loads(blob)
    return stats


def build(tracks: list[pathlib.Path], tempo: float, lufs: float) -> float:
    clips: list[pathlib.Path] = []
    total = 0.0

    for index, audio in enumerate(tracks):
        seconds = duration(audio) / tempo
        held = seconds + PAD
        total += held

        stats = measure(audio, CHAIN, lufs)
        norm = (
            f"loudnorm=I={lufs}:TP={TRUE_PEAK}:LRA=11"
            f":measured_I={stats['input_i']}:measured_TP={stats['input_tp']}"
            f":measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}"
            f":offset={stats['target_offset']}:linear=true"
        )
        chain = f"{CHAIN},{norm}"
        # atempo last among the time-domain filters: it changes pace without
        # shifting pitch, and doing it after normalisation keeps the measurement
        # valid because tempo does not change loudness.
        if abs(tempo - 1.0) > 1e-3:
            chain = f"{chain},atempo={tempo:.3f}"

        clip = HERE / f"clip_{index:02d}.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-loop",
                "1",
                "-framerate",
                "30",
                "-i",
                str(SLIDE_DIR / f"{index:02d}.png"),
                "-i",
                str(audio),
                "-filter_complex",
                f"[0:v]scale=1920:1080,format=yuv420p,"
                f"fade=t=in:st=0:d=0.35,fade=t=out:st={held - 0.35:.2f}:d=0.35[v];"
                f"[1:a]{chain},adelay=120|120,apad=pad_dur={PAD}[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-t",
                f"{held:.2f}",
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "320k",
                "-ar",
                "48000",
                str(clip),
            ],
            check=True,
        )
        clips.append(clip)

    listing = HERE / "concat.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            str(OUTPUT),
        ],
        check=True,
    )
    for clip in clips:
        clip.unlink()
    listing.unlink()
    return total


def main() -> None:
    tempo = 1.0
    lufs = DEFAULT_LUFS
    if "--tempo" in sys.argv:
        tempo = float(sys.argv[sys.argv.index("--tempo") + 1])
    if "--lufs" in sys.argv:
        lufs = float(sys.argv[sys.argv.index("--lufs") + 1])

    tracks = recordings()
    if tracks is None:
        raise SystemExit(
            f"no complete set of recordings in {VOICE_DIR.name}/ — expected "
            f"{len(SCRIPT)} files named 00..{len(SCRIPT) - 1:02d}. This video is "
            "narrated by a person; there is deliberately no synthetic fallback."
        )
    print(f"voice: {VOICE_DIR.name}/ — {len(tracks)} recordings")

    if "--fit" in sys.argv:
        # Solve for the pace that lands just under a hard limit, rather than
        # guessing a tempo and rebuilding until it fits.
        limit = float(sys.argv[sys.argv.index("--fit") + 1])
        speech = sum(duration(t) for t in tracks)
        tempo = max(1.0, speech / (limit - len(tracks) * PAD - 1.0))
        print(f"fit: {speech:.0f}s of speech into {limit:.0f}s → tempo {tempo:.3f}")

    total = build(tracks, tempo, lufs)
    print(
        f"{OUTPUT.name}  {int(total // 60)}:{int(total % 60):02d}  (tempo {tempo:.3f}, {lufs} LUFS)"
    )


if __name__ == "__main__":
    main()
