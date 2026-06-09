#!/bin/bash
cd "$(dirname "$0")"

python3 make_fcpxml_silence_cuts.py input.mp4 -o cuts.fcpxml \
  --adaptive-noise --auto-noise-margin 3 \
  --adaptive-window 300 \
  --adaptive-sample 20 \
  --adaptive-probes 3 \
  --min-silence 0.2 \
  --shrink-silence-pre 0 \
  --shrink-silence-post 0 \
  --pad-pre 0.25 \
  --pad-post 0.25 \
  --prefilter "highpass=f=50"
