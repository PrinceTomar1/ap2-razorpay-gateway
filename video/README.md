# The pitch video

`ap2-razorpay-pitch.mp4` — 4 minutes 49 seconds, 1920×1080, from the nine slides
in `../slides/` with narration over each.

**The voice is synthetic** (macOS `Aman`, en-IN). Said here plainly so nobody has
to wonder.

## Regenerating it

Everything is scripted, so the video is reproducible rather than hand-assembled:

```bash
cd video
python3 build.py          # narration → audio → per-slide clips → concat
```

- `narration.py` — the spoken script, one entry per slide. Edit this to change
  what is said; word counts drive the runtime.
- `slides/*.png` — frames captured from `../slides/index.html?clean` with headless
  Chrome at 1920×1080. `?clean` hides the deck's own nav chrome.
- `audio/*.aiff` — one narration track per slide, `say -v Aman -r 172`.
- `build.py` — measures each track, holds its slide for exactly that long plus a
  half-second beat, cross-fades, and concatenates.

Runtime is a function of the script: roughly `words / 172 × 60 + 4.5` seconds.
The current script is 796 words.
