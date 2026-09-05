# The pitch video

`ap2-razorpay-pitch.mp4` — **4:57**, 1920×1080, narrated by Prince Tomar over ten
slides.

The recordings in `voice/` run 5:50 at their natural pace, and the submission bar
is five minutes. `atempo` closes that gap: it shortens speech without shifting
pitch, so the voice is unchanged, it just moves along. `--fit` solves for the
tempo rather than guessing one.

## Rebuilding

```bash
cd video
python3 build.py                 # natural pace, whatever length that lands on
python3 build.py --fit 298       # solve for the tempo that fits five minutes
python3 build.py --lufs -16      # broadcast target instead of YouTube's -14
```

There is no synthetic fallback. If `voice/` is missing any of the ten clips the
build stops, because a video that is half one voice and half another is worse than
no video.

Each slide is held for exactly as long as its own audio, so re-recording one clip
changes that slide's timing and nothing else.

- `narration.py` — the script, one entry per slide. The words a person reads aloud
  (`../SPEAKING_SCRIPT.md`) are generated from this, so the two cannot drift.
- `voice/00.mp3` … `09.mp3` — the recordings.
- `slides/00.png` … `09.png` — frames from `../slides/index.html?clean`, captured
  with headless Chrome at 1920×1080. `?clean` hides the deck's nav chrome.

## The audio chain

Every clip runs through the same restoration chain, in this order, before it
reaches the timeline. The order is the point — each step assumes the previous one
has already run.

| | | why here |
|---|---|---|
| `pan=stereo` | dual-mono | first. These were two-mic recordings with the far capsule 4-5 dB down and 3 dB worse on SNR; a plain downmix averaged the good capture with the bad one |
| `highpass=f=85` | rumble | first, so nothing downstream spends headroom on energy nobody can hear |
| `afftdn=nf=-25` | denoise | gentle. Harder settings buy a few more dB and sound underwater |
| `equalizer 6.5 kHz −4 dB` | de-ess | sibilance lives here; a narrow notch beats a compressor that pumps the whole track |
| `acompressor 2.5:1` | evenness | quiet words come up, loud ones stop short of clipping, delivery still breathes |
| `equalizer 3.5 kHz +3 dB` | presence | the band that decides whether consonants land |
| `loudnorm` two-pass | level | measured target, applied linearly |
| `alimiter` | headroom | last. Buys several dB of level that peaks would otherwise block |

Measured on the finished file: room tone drops **4–5 dB** in the pauses;
**−11.2 LUFS, −1.3 dBTP**, LRA 2.4, zero clipped samples.

The number that actually mattered is the **mono downmix: −14.2 LUFS, up from
−18.3**. Every laptop and phone speaker sums to mono, and the original stereo pair
lost 4.1 dB when it did — so the track measured on target and was still hard to
hear on the devices people actually use. Dual-mono plus the limiter closed that.
`moov` leads the file (`+faststart`) so a player knows there is an audio track
before it has read 12 MB.

`loudnorm` runs twice on purpose. A single pass estimates from a running window
and drifts on clips this short; measuring first and correcting second costs one
extra decode and lands on the number.
