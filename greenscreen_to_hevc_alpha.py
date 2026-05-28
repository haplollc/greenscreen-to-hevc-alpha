#!/usr/bin/env python3
"""
Strip a green-screen background from a video and export as HEVC with alpha
(.mov, hvc1) — Apple's native transparent video format. Plays in AVPlayer
with one line of SwiftUI.

Pipeline:
  1. Extract every frame of the source to PNG.
  2. For each frame:
       - compute a per-pixel "greenness" score: G - (R+B)/2
       - high greenness → fully transparent (background)
       - mid greenness  → soft alpha falloff (clean anti-aliased edge)
       - any green tint → cap G channel to max(R, B) ("despill")
       - in fully-transparent areas, replace RGB with neutral white so
         alpha quantization can't bleed green back in during encoding
  3. Re-encode the PNG sequence via a ProRes 4444 intermediate (to handle
     the yuva pixel format) then transcode to HEVC + alpha (hvc1).

Why per-pixel python instead of ffmpeg's `chromakey`:
  - ffmpeg's chromakey leaves a visible green fringe on the edges
  - despill via ffmpeg's `despill` filter shifts the WHOLE image's green
    channel, not just the edges, which makes brown / orange characters
    look weird
  - this script touches only pixels that have a green bias, leaves
    everything else alone

Requirements:
  - ffmpeg (with hevc_videotoolbox — Apple silicon / Intel Mac)
  - Python 3 + Pillow + numpy + scipy

Usage:
  python3 greenscreen_to_hevc_alpha.py input.mp4 output.mov
"""

import os
import sys
import tempfile
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, label as cc_label


# --- Tunables ---------------------------------------------------------------

STRONG_GREEN = 50         # greenness ≥ this → fully transparent
SOFT_GREEN   = 5          # greenness ≤ this → fully opaque; in between fades
ALPHA_BLUR_SIGMA = 1.0    # smooths the alpha edge after thresholding
BLACK_LUM = 30            # luminance below this and touching the frame edge =
                          # letterbox bar → also transparent. Set None to skip.


def process_frame(in_path: Path, out_path: Path) -> None:
    im = np.array(Image.open(in_path).convert("RGB")).astype(np.float32)
    R, G, B = im[..., 0], im[..., 1], im[..., 2]
    greenness = G - (R + B) * 0.5

    # Alpha curve: 1.0 → 0.0 between SOFT_GREEN and STRONG_GREEN
    alpha = np.clip((STRONG_GREEN - greenness) / (STRONG_GREEN - SOFT_GREEN),
                    0.0, 1.0)

    # Black-letterbox handling: any near-black region connected to the frame
    # edge is letterbox (not part of the subject). Mask out.
    if BLACK_LUM is not None:
        lum = 0.299 * R + 0.587 * G + 0.114 * B
        labeled, _ = cc_label(lum < BLACK_LUM)
        edge_labels = set(labeled[0, :]) | set(labeled[-1, :]) \
                    | set(labeled[:, 0]) | set(labeled[:, -1])
        edge_labels.discard(0)
        if edge_labels:
            alpha[np.isin(labeled, list(edge_labels))] = 0.0

    # Light blur on the alpha so edges aren't pixel-stair-steppy
    alpha = gaussian_filter(alpha, sigma=ALPHA_BLUR_SIGMA)
    alpha = (np.clip(alpha, 0, 1) * 255).astype(np.uint8)

    # Despill: any pixel with positive green bias gets its G channel capped
    # to max(R, B). Kills the residual tint without affecting non-green pixels.
    G_capped = np.minimum(G, np.maximum(R, B))
    G_out = np.where(greenness > 0, G_capped, G)

    # In fully-transparent areas, replace RGB with neutral white so any
    # alpha quantization during encoding can't bleed green back in.
    bg = alpha < 4
    R = np.where(bg, 255, R)
    G_out = np.where(bg, 255, G_out)
    B = np.where(bg, 255, B)

    rgb = np.stack([R, G_out, B], axis=-1).astype(np.uint8)
    Image.fromarray(np.dstack([rgb, alpha]), mode="RGBA").save(out_path)


def run(cmd: list[str]) -> None:
    """Run a subprocess command and exit on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python3 greenscreen_to_hevc_alpha.py input.mp4 output.mov",
              file=sys.stderr)
        sys.exit(2)

    src = Path(sys.argv[1]).expanduser().resolve()
    out = Path(sys.argv[2]).expanduser().resolve()
    if not src.exists():
        print(f"input not found: {src}", file=sys.stderr)
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        frames_in  = tmp / "in"
        frames_out = tmp / "out"
        frames_in.mkdir()
        frames_out.mkdir()

        print("→ extracting frames...")
        run(["ffmpeg", "-y", "-i", str(src),
             str(frames_in / "f%05d.png")])

        in_pngs = sorted(frames_in.glob("*.png"))
        print(f"→ processing {len(in_pngs)} frames...")
        for i, p in enumerate(in_pngs):
            process_frame(p, frames_out / p.name)
            if (i + 1) % 24 == 0:
                print(f"   {i + 1}/{len(in_pngs)}")

        # Probe the source frame rate
        fps_probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "default=nw=1:nk=1",
             str(src)],
            capture_output=True, text=True,
        ).stdout.strip()
        try:
            num, den = fps_probe.split("/")
            fps = float(num) / float(den)
        except Exception:
            fps = 30.0

        print(f"→ encoding ProRes 4444 intermediate at {fps:g} fps...")
        inter = tmp / "intermediate.mov"
        run(["ffmpeg", "-y",
             "-framerate", str(fps),
             "-i", str(frames_out / "f%05d.png"),
             "-vf", "format=yuva444p10le",
             "-c:v", "prores_ks", "-profile:v", "4444",
             "-pix_fmt", "yuva444p10le",
             str(inter)])

        print("→ transcoding to HEVC with alpha...")
        run(["ffmpeg", "-y", "-i", str(inter),
             "-an",
             "-c:v", "hevc_videotoolbox",
             "-allow_sw", "1",
             "-alpha_quality", "0.92",
             "-tag:v", "hvc1",
             "-q:v", "55",
             str(out)])

        size_kb = out.stat().st_size / 1024
        print(f"✓ done → {out}  ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
