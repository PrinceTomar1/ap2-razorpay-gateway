"""Build the pitch video from the slides and the narration.

Deliberately dumb and reproducible. Each slide is held for exactly as long as its
own audio, so re-recording a line changes the timing and nothing else needs
touching.

Audio comes from ``voice/`` when a complete set of recordings is there, and from
the synthetic fallback otherwise. Real always wins: a pitch is somebody explaining
their own build, and a generated read is a stand-in for that rather than a
substitute. The fallback needs the *whole* set, so a half-finished recording
session can never silently ship a video that is half one voice and half another.

    python3 build.py                 # natural pace
    python3 build.py --tempo 1.18    # same voice, brisker, for a hard time limit
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from narration import SCRIPT  # noqa: E402

HERE = pathlib.Path(__file__).parent
VOICE_DIR = HERE / "voice"
SYNTH_DIR = HERE / "audio"
SLIDE_DIR = HERE / "slides"
OUTPUT = HERE / "ap2-razorpay-pitch.mp4"

SYNTH_VOICE = "Aman"  # en-IN, fallback only
SYNTH_RATE = 172  # words per minute
PAD = 0.4  # a beat of silence after each slide
AUDIO_EXTS = ("mp3", "m4a", "wav", "aiff", "aac", "flac")


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


def synthesise() -> list[pathlib.Path]:
    """The fallback voice, one track per slide."""
    SYNTH_DIR.mkdir(exist_ok=True)
    paths: list[pathlib.Path] = []
    for name, text in SCRIPT:
        path = SYNTH_DIR / f"{name}.aiff"
        subprocess.run(
            ["say", "-v", SYNTH_VOICE, "-r", str(SYNTH_RATE), "-o", str(path), " ".join(text.split())],
            check=True,
        )
        paths.append(path)
    return paths


def build(tracks: list[pathlib.Path], tempo: float) -> float:
    clips: list[pathlib.Path] = []
    total = 0.0

    for index, audio in enumerate(tracks):
        seconds = duration(audio) / tempo
        held = seconds + PAD
        total += held

        # loudnorm evens out level between clips recorded at different distances
        # from the mic in one sitting; atempo changes pace without shifting pitch.
        chain = "loudnorm=I=-16:TP=-1.5:LRA=11"
        if abs(tempo - 1.0) > 1e-3:
            chain = f"atempo={tempo:.3f},{chain}"

        clip = HERE / f"clip_{index:02d}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-loop", "1", "-framerate", "30",
                "-i", str(SLIDE_DIR / f"{index:02d}.png"),
                "-i", str(audio),
                "-filter_complex",
                f"[0:v]scale=1920:1080,format=yuv420p,"
                f"fade=t=in:st=0:d=0.35,fade=t=out:st={held - 0.35:.2f}:d=0.35[v];"
                f"[1:a]{chain},adelay=120|120,apad=pad_dur={PAD}[a]",
                "-map", "[v]", "-map", "[a]", "-t", f"{held:.2f}",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                str(clip),
            ],
            check=True,
        )
        clips.append(clip)

    listing = HERE / "concat.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(OUTPUT)],
        check=True,
    )
    for clip in clips:
        clip.unlink()
    listing.unlink()
    return total


def main() -> None:
    tempo = 1.0
    if "--tempo" in sys.argv:
        tempo = float(sys.argv[sys.argv.index("--tempo") + 1])

    tracks = recordings()
    if tracks is not None:
        print(f"voice: {VOICE_DIR.name}/ — {len(tracks)} recordings")
    else:
        print(f"voice: no complete set in {VOICE_DIR.name}/, synthesising")
        tracks = synthesise()

    total = build(tracks, tempo)
    print(f"{OUTPUT.name}  {int(total // 60)}:{int(total % 60):02d}  (tempo {tempo})")


if __name__ == "__main__":
    main()
