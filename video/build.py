"""Build the pitch video from the slides and the narration script.

Deliberately dumb and reproducible: each slide is held for exactly as long as its
own narration takes, so re-writing a line in narration.py changes the timing and
nothing else needs touching.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from narration import SCRIPT

VOICE = "Aman"  # en-IN
RATE = 172  # words per minute
PAD = 0.5  # a beat of silence after each slide
HERE = pathlib.Path(__file__).parent


def narrate() -> list[tuple[str, float]]:
    audio = HERE / "audio"
    audio.mkdir(exist_ok=True)
    out: list[tuple[str, float]] = []
    for name, text in SCRIPT:
        path = audio / f"{name}.aiff"
        subprocess.run(
            ["say", "-v", VOICE, "-r", str(RATE), "-o", str(path), " ".join(text.split())],
            check=True,
        )
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        out.append((name, float(json.loads(probe)["format"]["duration"])))
    return out


def build(durations: list[tuple[str, float]]) -> None:
    clips = []
    for index, (name, seconds) in enumerate(durations):
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
                str(HERE / f"slides/{index:02d}.png"),
                "-i",
                str(HERE / f"audio/{name}.aiff"),
                "-filter_complex",
                f"[0:v]scale=1920:1080,format=yuv420p,fade=t=in:st=0:d=0.35,"
                f"fade=t=out:st={seconds + PAD - 0.35:.2f}:d=0.35[v];"
                f"[1:a]adelay=150|150,apad=pad_dur={PAD}[a]",
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-t",
                f"{seconds + PAD:.2f}",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "44100",
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
            str(HERE / "ap2-razorpay-pitch.mp4"),
        ],
        check=True,
    )
    for clip in clips:
        clip.unlink()
    listing.unlink()


if __name__ == "__main__":
    durations = narrate()
    build(durations)
    total = sum(d + PAD for _, d in durations)
    print(f"ap2-razorpay-pitch.mp4  {int(total // 60)}:{int(total % 60):02d}")
