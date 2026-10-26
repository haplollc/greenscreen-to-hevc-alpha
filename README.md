# greenscreen-to-hevc-alpha

Strip a green-screen background from a video and export as **HEVC with alpha** (.mov, hvc1) — Apple's native transparent-video format. Plays in `AVPlayer` with one line of SwiftUI.

Originally written to ship a looping mascot animation in [Barrier](https://apps.apple.com/app/barrier) — the AI-generated source had a green background, this script removes it and packs the result down to a couple MB so it can ride in the app bundle.

<p align="center">
  <a href="https://buymeacoffee.com/jcbuilds"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" width="200"></a>
  <br>
  <sub>If this script is useful to you, a coffee keeps it going.</sub>
</p>

## Pipeline

1. Extract every frame of the source to PNG.
2. Per frame:
   - Compute a per-pixel "greenness" score: `G - (R+B)/2`
   - High greenness → fully transparent (background)
   - Mid greenness  → soft alpha falloff (clean anti-aliased edge)
   - Any green tint → cap the green channel to `max(R, B)` ("despill")
   - In fully-transparent areas, replace RGB with neutral white so alpha quantization can't bleed green back in during encoding
3. Re-encode the PNG sequence via a ProRes 4444 intermediate (handles the yuva pixel format), then transcode to HEVC + alpha (`hvc1`).

## Why per-pixel Python instead of `ffmpeg -vf chromakey,despill`

- `ffmpeg`'s `chromakey` leaves a visible green fringe on the edges.
- `ffmpeg`'s `despill` shifts the WHOLE image's green channel, not just the edges — brown / orange characters end up looking pink.
- This script only touches pixels that actually have a green bias, leaves everything else alone.

## Requirements

- `ffmpeg` with `hevc_videotoolbox` (ships on Apple Silicon / Intel Mac builds of FFmpeg)
- Python 3
- `pip install pillow numpy scipy`

## Usage

```bash
python3 greenscreen_to_hevc_alpha.py input.mp4 output.mov
```

Output: `.mov` with HEVC + alpha. Drop into Xcode, load with `AVPlayer`, layer over any background.

## Tunables (top of file)

| knob | default | effect |
|---|---|---|
| `STRONG_GREEN` | 50 | greenness ≥ this is fully transparent |
| `SOFT_GREEN` | 5 | greenness ≤ this is fully opaque; in between fades |
| `ALPHA_BLUR_SIGMA` | 1.0 | smooths the alpha edge after thresholding |
| `BLACK_LUM` | 30 | letterbox bars connected to the frame edge get masked off |

## File size

For reference: an 8-second 540×540 looping clip with full transparency comes out to ~2.5 MB. The equivalent ProRes 4444 with alpha is ~25 MB.
