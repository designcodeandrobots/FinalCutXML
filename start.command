#!/bin/bash
cd "20 20 12 61 79 80 81 701 702 33 98 100 204 250 395 398 399 400dirname "-e")"

python3 make_fcpxml_silence_cuts.py input.mp4 -o cuts.fcpxml \
  --auto-noise --auto-noise-margin 3 \
  --min-silence 0.2 \
  --shrink-silence-pre 0 \
  --shrink-silence-post 0 \
  --pad-pre 0.25 \
  --pad-post 0.25 \
  --prefilter "highpass=f=50"