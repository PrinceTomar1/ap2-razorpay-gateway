# The pitch video

Two builds of the same recording:

| File | Length | |
|---|---|---|
| `ap2-razorpay-pitch.mp4` | **4:56** | 1.20× pace, fits the five-minute bar |
| `ap2-razorpay-pitch-natural.mp4` | 5:54 | unaltered pace |

Both use Prince Tomar's own narration from `voice/`. The pace change is `atempo`,
which shortens speech without shifting pitch — the voice is unchanged, it just
moves along.

## Rebuilding

```bash
cd video
python3 build.py                 # natural pace
python3 build.py --tempo 1.20    # fits five minutes
```

`build.py` prefers the recordings in `voice/` and falls back to a synthetic voice
only when the set is incomplete — so a half-finished recording session cannot
silently ship a video that is half one voice and half another.

Each slide is held for exactly as long as its own audio, so re-recording one clip
changes that slide's timing and nothing else.

- `narration.py` — the script, one entry per slide. The words a person reads aloud
  (`../SPEAKING_SCRIPT.md`) are generated from this, so the two cannot drift.
- `voice/00.mp3` … `09.mp3` — the recordings.
- `slides/00.png` … `09.png` — frames from `../slides/index.html?clean`, captured
  with headless Chrome at 1920×1080. `?clean` hides the deck's nav chrome.
- Audio is loudness-normalised per clip (`loudnorm I=-16`), which evens out level
  between takes recorded at slightly different distances from the mic.
