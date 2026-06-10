#!/usr/bin/env python3
"""
make_fcpxml_silence_cuts.py

* Finds pauses with ffmpeg silencedetect and builds a timeline from non-silent segments.
* Quantizes all timings to frame boundaries and serializes them as rational seconds.
* Supports a manual noise threshold (--noise), automatic threshold detection (--auto-noise),
  or windowed adaptive threshold detection (--adaptive-noise).
* Protects word edges:
  - Outward rounding: start=floor, end=ceil
  - Asymmetric clip padding: --pad-pre / --pad-post
  - Silence shrinking before inversion: --shrink-silence-pre / --shrink-silence-post
* Optional detector prefilter: --prefilter "highpass=f=120,lowpass=f=8000"

Example (1080p30, short 0.5s pauses, protected word edges):
  python3 make_fcpxml_silence_cuts.py input.mp4 -o cuts.fcpxml \
    --auto-noise --min-silence 0.5 \
    --format-width 1920 --format-height 1080 --format-fps-num 30 --format-fps-den 1 \
    --shrink-silence-pre 0.04 --shrink-silence-post 0.12 \
    --pad-pre 0.06 --pad-post 0.16
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple, Optional
from urllib.parse import quote

# ---------- Utils ----------

def run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)

def which(name: str) -> bool:
    from shutil import which as _which
    return _which(name) is not None

def fail(msg: str, code: int = 1):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(code)

def clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)

def percentile(values: List[float], q: float) -> float:
    if not values:
        raise ValueError("percentile() requires at least one value")
    q = clamp(q, 0.0, 1.0)
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return ordered[lower]
    weight = pos - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

def fraction_for_framerate(rate_str: str) -> Tuple[int, int]:
    """ffprobe avg/r_frame_rate -> (num, den)"""
    if not rate_str:
        return (25, 1)
    if "/" in rate_str:
        a, b = rate_str.split("/")
        try:
            n, d = int(a), int(b)
            if n > 0 and d > 0:
                return (n, d)
        except Exception:
            pass
    try:
        n = int(round(float(rate_str)))
        if n > 0:
            return (n, 1)
    except Exception:
        pass
    return (25, 1)

def frames_floor(t: float, fps_num: int, fps_den: int) -> int:
    """Round time down to the nearest frame without cutting the start."""
    return int(math.floor((t * fps_num / fps_den) + 1e-9))

def frames_ceil(t: float, fps_num: int, fps_den: int) -> int:
    """Round time up to the nearest frame without cutting the end."""
    return int(math.ceil((t * fps_num / fps_den) - 1e-9))

def seconds_rational_from_frames(frames: int, fps_num: int, fps_den: int) -> Tuple[int, int]:
    return frames * fps_den, fps_num

def fmt_frames_as_seconds(frames: int, fps_num: int, fps_den: int) -> str:
    num, den = seconds_rational_from_frames(frames, fps_num, fps_den)
    return f"{num}/{den}s"

def fcpxml_format_name(width: int, height: int, fps_num: int, fps_den: int) -> str:
    """Return a Final Cut-friendly video format name for common timelines."""
    fps = fps_num / fps_den
    common_rates = {
        (24000, 1001): "23.98",
        (24, 1): "24",
        (25, 1): "25",
        (30000, 1001): "29.97",
        (30, 1): "30",
        (50, 1): "50",
        (60000, 1001): "59.94",
        (60, 1): "60",
    }
    rate = common_rates.get((fps_num, fps_den), f"{fps:g}")
    standard_heights = {720, 1080, 1440, 2160, 4320}
    size = f"{height}" if height in standard_heights else f"{width}x{height}"
    return f"FFVideoFormat{size}p{rate}"

# ---------- Media Info ----------

@dataclass
class MediaInfo:
    src_url: str
    duration: float
    width: int
    height: int
    fps_num: int
    fps_den: int
    has_audio: bool

def get_media_info(path: Path) -> MediaInfo:
    if not which("ffprobe"):
        fail("ffprobe was not found. Install FFmpeg and make sure ffprobe is in PATH.")
    p = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(path)])
    if p.returncode != 0:
        fail(f"ffprobe error: {p.stderr.strip() or p.stdout.strip()}")
    info = json.loads(p.stdout)
    duration = float(info.get("format", {}).get("duration", 0.0))
    v_width, v_height = 1920, 1080
    fps_n, fps_d = 25, 1
    has_audio = False
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            v_width = int(s.get("width", v_width))
            v_height = int(s.get("height", v_height))
            fps_n, fps_d = fraction_for_framerate(s.get("avg_frame_rate") or s.get("r_frame_rate") or "25")
        if s.get("codec_type") == "audio":
            has_audio = True
    src_url = "file://" + quote(path.resolve().as_posix())
    return MediaInfo(src_url, duration, v_width, v_height, fps_n, fps_d, has_audio)

# ---------- Silence Detection ----------

@dataclass
class Interval:
    start: float
    end: float
    @property
    def dur(self) -> float:
        return max(0.0, self.end - self.start)

SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9]+\.?[0-9]*)")
SILENCE_END_RE   = re.compile(r"silence_end:\s*([0-9]+\.?[0-9]*)")
TIME_RE          = re.compile(r"time=(\d+):(\d+):(\d+\.?\d*)")

def _hms_to_seconds(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)

def _print_progress(cur: float, total: float, label: str = "Analyze"):
    import shutil
    width = max(20, shutil.get_terminal_size((80, 20)).columns - 20)
    pct = 0.0 if total <= 0 else min(1.0, max(0.0, cur / total))
    filled = int(pct * width)
    bar = "█" * filled + "·" * (width - filled)
    sys.stdout.write(f"\r{label} [{bar}] {pct*100:5.1f}%")
    sys.stdout.flush()

def detect_silences(path: Path, noise_db: float, min_silence: float, *,
                    total_dur: float = 0.0, show_progress: bool = True, prefilter: str = "",
                    start_time: float = 0.0, duration: Optional[float] = None,
                    progress_offset: float = 0.0, progress_label: str = "Analyze") -> List[Interval]:
    if not which("ffmpeg"):
        fail("ffmpeg was not found. Install FFmpeg and make sure ffmpeg is in PATH.")
    chain = []
    if prefilter.strip():
        chain.append(prefilter.strip())
    chain.append(f"silencedetect=noise={noise_db}dB:d={min_silence}")
    filt = ",".join(chain)
    cmd = ["ffmpeg", "-hide_banner"]
    if start_time > 0:
        cmd += ["-ss", f"{start_time:.6f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-af", filt, "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    silences: List[Interval] = []
    current_start: Optional[float] = None
    last_time = 0.0
    segment_end = start_time + duration if duration is not None else None
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            t = TIME_RE.search(line)
            if t:
                last_time = _hms_to_seconds(*t.groups())
                if show_progress:
                    _print_progress(progress_offset + last_time, total_dur or last_time, progress_label)
            m1 = SILENCE_START_RE.search(line)
            if m1:
                current_start = start_time + float(m1.group(1))
                if segment_end is not None:
                    current_start = min(max(start_time, current_start), segment_end)
                continue
            m2 = SILENCE_END_RE.search(line)
            if m2 and current_start is not None:
                silence_end = start_time + float(m2.group(1))
                if segment_end is not None:
                    silence_end = min(max(start_time, silence_end), segment_end)
                if silence_end > current_start:
                    silences.append(Interval(current_start, silence_end))
                current_start = None
    finally:
        proc.wait()
        if current_start is not None and segment_end is not None:
            silences.append(Interval(current_start, min(segment_end, total_dur or segment_end)))
        if show_progress:
            done = progress_offset + (duration if duration is not None else last_time)
            _print_progress(total_dur if duration is None else done, total_dur or last_time, progress_label)
            sys.stdout.write("\n")
    return merge_overlaps(silences)

def detect_silences_adaptive(path: Path, min_silence: float, *,
                             total_dur: float,
                             margin_db: float,
                             window_dur: float,
                             sample_dur: float,
                             probe_count: int,
                             probe_quantile: float,
                             min_noise_db: float,
                             max_noise_db: float,
                             fallback_noise_db: float,
                             show_progress: bool = True,
                             prefilter: str = "") -> List[Interval]:
    if window_dur <= 0:
        fail("--adaptive-window must be greater than 0")
    if sample_dur <= 0:
        fail("--adaptive-sample must be greater than 0")
    if probe_count <= 0:
        fail("--adaptive-probes must be greater than 0")

    silences: List[Interval] = []
    pos = 0.0
    window_index = 1
    while pos < total_dur - 1e-6:
        dur = min(window_dur, total_dur - pos)
        est = estimate_window_noise_floor_db(
            path,
            window_start=pos,
            window_dur=dur,
            sample_dur=sample_dur,
            probe_count=probe_count,
            probe_quantile=probe_quantile,
            prefilter=prefilter,
        )
        noise_db = (est + margin_db) if est is not None else fallback_noise_db
        noise_db = clamp(noise_db, min_noise_db, max_noise_db)
        if show_progress:
            print(
                f"[adaptive-noise] window {window_index}: "
                f"{pos:.1f}s-{pos + dur:.1f}s, threshold --noise {noise_db:.1f} dB"
            )
        silences.extend(
            detect_silences(
                path,
                noise_db=noise_db,
                min_silence=min_silence,
                total_dur=total_dur,
                show_progress=show_progress,
                prefilter=prefilter,
                start_time=pos,
                duration=dur,
                progress_offset=pos,
                progress_label="Analyze",
            )
        )
        pos += dur
        window_index += 1
    return merge_overlaps(silences)

def merge_overlaps(intervals: List[Interval]) -> List[Interval]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: (x.start, x.end))
    merged = [intervals[0]]
    for iv in intervals[1:]:
        last = merged[-1]
        if iv.start <= last.end + 1e-6:
            last.end = max(last.end, iv.end)
        else:
            merged.append(Interval(iv.start, iv.end))
    return merged

def shrink_silences(intervals: List[Interval], total: float, *,
                    shrink_pre: float, shrink_post: float) -> List[Interval]:
    """Shrink each silence interval by moving its start forward and its end backward."""
    if not intervals:
        return []
    out: List[Interval] = []
    for iv in intervals:
        s = min(max(0.0, iv.start + max(0.0, shrink_pre)), total)
        e = min(max(0.0, iv.end   - max(0.0, shrink_post)), total)
        if e - s > 1e-6:
            out.append(Interval(s, e))
        # If the silence collapsed, skip it and treat it as non-silence.
    return merge_overlaps(out)

def invert_intervals(intervals: List[Interval], total: float) -> List[Interval]:
    if total <= 0:
        return []
    if not intervals:
        return [Interval(0.0, total)]
    out: List[Interval] = []
    prev = 0.0
    for iv in intervals:
        if iv.start - prev > 1e-6:
            out.append(Interval(prev, iv.start))
        prev = iv.end
    if total - prev > 1e-6:
        out.append(Interval(prev, total))
    return out

def pad_and_filter(intervals: List[Interval], total: float, *,
                   pad: float, min_clip: float,
                   pad_pre: Optional[float] = None, pad_post: Optional[float] = None) -> List[Interval]:
    """Apply asymmetric padding: pre before the start, post after the end."""
    if not intervals:
        return []
    pre = pad if pad_pre is None else pad_pre
    post = pad if pad_post is None else pad_post
    padded: List[Interval] = []
    for iv in intervals:
        start = max(0.0, iv.start - pre)
        end   = min(total, iv.end + post)
        if end > start:
            padded.append(Interval(start, end))
    padded = merge_overlaps(padded)
    return [iv for iv in padded if iv.dur >= min_clip]

# ---------- Noise floor estimation ----------

def estimate_noise_floor_db(path: Path, sample_dur: float = 30.0, *,
                            start_time: float = 0.0, prefilter: str = "") -> Optional[float]:
    """Estimate mean_volume in dBFS from a sample window."""
    if not which("ffmpeg"):
        return None
    chain = []
    if prefilter.strip():
        chain.append(prefilter.strip())
    chain.append("volumedetect")
    cmd = ["ffmpeg", "-hide_banner"]
    if start_time > 0:
        cmd += ["-ss", f"{start_time:.6f}"]
    cmd += ["-i", str(path), "-t", str(sample_dur), "-af", ",".join(chain), "-f", "null", "-"]
    p = run(cmd)
    m = re.search(r"mean_volume:\s*(-?\d+(\.\d+)?) dB", p.stderr)
    if not m:
        return None
    return float(m.group(1))

def estimate_window_noise_floor_db(path: Path, *, window_start: float, window_dur: float,
                                   sample_dur: float, probe_count: int,
                                   probe_quantile: float,
                                   prefilter: str = "") -> Optional[float]:
    """Estimate local noise floor by sampling several positions and taking a low percentile."""
    sample = min(sample_dur, window_dur)
    if sample <= 0:
        return None
    if probe_count == 1 or window_dur <= sample:
        offsets = [0.0]
    else:
        step = (window_dur - sample) / (probe_count - 1)
        offsets = [step * i for i in range(probe_count)]

    estimates: List[float] = []
    for offset in offsets:
        est = estimate_noise_floor_db(
            path,
            sample_dur=sample,
            start_time=window_start + offset,
            prefilter=prefilter,
        )
        if est is not None:
            estimates.append(est)
    if not estimates:
        return None
    return percentile(estimates, probe_quantile)

# ---------- FCPXML ----------

def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )

def build_fcpxml(
    mi: MediaInfo,
    clips: List[Interval],
    project_name: str,
    *,
    timeline_width: int,
    timeline_height: int,
    timeline_fps_num: int,
    timeline_fps_den: int,
) -> str:
    # Convert clips to frames with outward rounding.
    start_frames: List[int] = []
    dur_frames: List[int] = []
    for c in clips:
        sf = frames_floor(c.start, timeline_fps_num, timeline_fps_den)
        ef = frames_ceil(c.end,   timeline_fps_num, timeline_fps_den)
        df = max(0, ef - sf)
        if df <= 0:
            continue
        start_frames.append(sf)
        dur_frames.append(df)

    # Cumulative offsets.
    offsets: List[int] = []
    acc = 0
    for df in dur_frames:
        offsets.append(acc)
        acc += df
    timeline_frames = acc

    frame_duration_str = fmt_frames_as_seconds(1, timeline_fps_num, timeline_fps_den)

    # Build the spine.
    spine_items: List[str] = []
    for i, (sf, of, df) in enumerate(zip(start_frames, offsets, dur_frames), 1):
        spine_items.append(
            f'            <asset-clip name="Segment {i}" ref="r2" '
            f'start="{fmt_frames_as_seconds(sf, timeline_fps_num, timeline_fps_den)}" '
            f'offset="{fmt_frames_as_seconds(of, timeline_fps_num, timeline_fps_den)}" '
            f'duration="{fmt_frames_as_seconds(df, timeline_fps_num, timeline_fps_den)}"/>'
        )
    spine_xml = "\n".join(spine_items) if spine_items else "            <!-- No clips -->"

    # Resources and sequence.
    asset_duration_frames = frames_ceil(mi.duration, timeline_fps_num, timeline_fps_den)
    format_line = (
        f'    <format id="r1" name="{fcpxml_format_name(timeline_width, timeline_height, timeline_fps_num, timeline_fps_den)}" frameDuration="{frame_duration_str}" '
        f'width="{timeline_width}" height="{timeline_height}"/>'
    )
    sequence_open = (
        f'        <sequence format="r1" '
        f'duration="{fmt_frames_as_seconds(timeline_frames, timeline_fps_num, timeline_fps_den)}" '
        f'tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">'
    )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.10">
  <resources>
{format_line}
    <asset id="r2" name="source" start="0s"
           duration="{fmt_frames_as_seconds(asset_duration_frames, timeline_fps_num, timeline_fps_den)}"
           hasVideo="1" hasAudio="{1 if mi.has_audio else 0}" format="r1">
      <media-rep kind="original-media" src="{mi.src_url}"/>
    </asset>
  </resources>
  <library>
    <event name="{xml_escape(project_name)}">
      <project name="{xml_escape(project_name)}">
{sequence_open}
          <spine>
{spine_xml}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""
    return xml

# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="FCPXML silence-cut timeline generator")
    ap.add_argument("input", type=Path, help="Path to the input video file")
    ap.add_argument("-o", "--out", type=Path, default=None, help="Where to save the .fcpxml file (default: next to the video)")
    ap.add_argument("--name", default=None, help="Project name (default: input file name)")

    # Silence detector.
    ap.add_argument("--noise", type=float, default=-35.0, help="Silence threshold in dBFS (unless auto-noise is enabled)")
    ap.add_argument("--auto-noise", dest="auto_noise", action="store_true", help="Estimate the silence threshold automatically")
    ap.add_argument("--auto-noise-margin", dest="auto_noise_margin", type=float, default=5.0,
                    help="Margin in dB added to the estimated noise floor")
    ap.add_argument("--adaptive-noise", dest="adaptive_noise", action="store_true",
                    help="Estimate and apply a separate silence threshold for each time window")
    ap.add_argument("--adaptive-window", type=float, default=300.0,
                    help="Window duration in seconds for adaptive noise detection")
    ap.add_argument("--adaptive-sample", type=float, default=20.0,
                    help="Sample duration in seconds used to estimate each adaptive window")
    ap.add_argument("--adaptive-probes", type=int, default=7,
                    help="Number of sample probes per adaptive window")
    ap.add_argument("--adaptive-quantile", type=float, default=0.35,
                    help="Probe percentile used as local noise floor; 0 is quietest, 1 is loudest")
    ap.add_argument("--adaptive-min-noise", type=float, default=-80.0,
                    help="Lower clamp for adaptive silence thresholds in dB")
    ap.add_argument("--adaptive-max-noise", type=float, default=-35.0,
                    help="Upper clamp for adaptive silence thresholds in dB")
    ap.add_argument("--min-silence", type=float, default=0.6, help="Minimum silence duration in seconds")

    # Optional detector prefilter.
    ap.add_argument("--prefilter", default="", help="FFmpeg filters before silencedetect, e.g. 'highpass=f=120,lowpass=f=8000'")

    # Speech post-processing.
    ap.add_argument("--min-clip", type=float, default=0.3, help="Minimum clip duration in seconds")
    ap.add_argument("--pad", type=float, default=0.05, help="Base padding before/after each clip in seconds")
    ap.add_argument("--pad-pre", type=float, default=None, help="Padding before each clip (overrides --pad)")
    ap.add_argument("--pad-post", type=float, default=None, help="Padding after each clip (overrides --pad)")
    ap.add_argument("--shrink-silence-pre", type=float, default=0.0, help="Shorten the start of each silence by N seconds")
    ap.add_argument("--shrink-silence-post", type=float, default=0.0, help="Shorten the end of each silence by N seconds")

    ap.add_argument("--debug", action="store_true", help="Print detected silences and generated clips")
    ap.add_argument("--no-progress", action="store_true", help="Disable the progress bar")

    # Timeline format.
    ap.add_argument("--format-width",  type=int, default=None, help="Timeline width, e.g. 1920")
    ap.add_argument("--format-height", type=int, default=None, help="Timeline height, e.g. 1080")
    ap.add_argument("--format-fps-num", type=int, default=None, help="FPS numerator, e.g. 30")
    ap.add_argument("--format-fps-den", type=int, default=None, help="FPS denominator, e.g. 1")

    args = ap.parse_args()

    if not args.input.exists():
        fail(f"File not found: {args.input}")

    mi = get_media_info(args.input)

    # Auto-detect the noise floor for the non-adaptive path.
    if args.auto_noise and not args.adaptive_noise and mi.has_audio:
        est = estimate_noise_floor_db(args.input, prefilter=args.prefilter)
        if est is not None:
            args.noise = est + args.auto_noise_margin
            print(f"[auto-noise] noise floor ~= {est:.1f} dBFS -> silence threshold --noise {args.noise:.1f} dB")

    # 1) Detect silences, optionally with a prefilter.
    if not mi.has_audio:
        print("Warning: the file has no audio track. A single full-duration clip will be generated.")
        clips = [Interval(0.0, mi.duration)]
        silences: List[Interval] = []
    else:
        if args.adaptive_noise:
            silences = detect_silences_adaptive(
                args.input,
                min_silence=args.min_silence,
                total_dur=mi.duration,
                margin_db=args.auto_noise_margin,
                window_dur=args.adaptive_window,
                sample_dur=args.adaptive_sample,
                probe_count=args.adaptive_probes,
                probe_quantile=args.adaptive_quantile,
                min_noise_db=args.adaptive_min_noise,
                max_noise_db=args.adaptive_max_noise,
                fallback_noise_db=args.noise,
                show_progress=not args.no_progress,
                prefilter=args.prefilter
            )
        else:
            silences = detect_silences(
                args.input,
                noise_db=args.noise,
                min_silence=args.min_silence,
                total_dur=mi.duration,
                show_progress=not args.no_progress,
                prefilter=args.prefilter
            )

        # 2) Shrink silences to expand speech regions.
        silences = shrink_silences(
            silences, mi.duration,
            shrink_pre=args.shrink_silence_pre,
            shrink_post=args.shrink_silence_post
        )

        # 3) Invert silences into speech regions and add padding.
        speech = invert_intervals(silences, mi.duration)
        clips = pad_and_filter(
            speech, mi.duration,
            pad=args.pad, min_clip=args.min_clip,
            pad_pre=args.pad_pre, pad_post=args.pad_post
        )

    if args.debug and mi.has_audio:
        print("\nSilences after shrink:")
        for iv in silences:
            print(f"  S: {iv.start:.3f} — {iv.end:.3f} ({iv.dur:.3f}s)")
        print("\nClips:")
        for i, iv in enumerate(clips, 1):
            print(f"  {i:02d}: {iv.start:.3f} — {iv.end:.3f} (dur {iv.dur:.3f}s)")

    # Timeline format.
    timeline_width  = args.format_width  or mi.width
    timeline_height = args.format_height or mi.height
    timeline_fps_n  = args.format_fps_num or mi.fps_num
    timeline_fps_d  = args.format_fps_den or mi.fps_den

    # XML.
    name = args.name or args.input.stem
    xml = build_fcpxml(
        mi, clips,
        project_name=name,
        timeline_width=timeline_width,
        timeline_height=timeline_height,
        timeline_fps_num=timeline_fps_n,
        timeline_fps_den=timeline_fps_d,
    )

    out_path = args.out or args.input.with_suffix(".fcpxml")
    out_path.write_text(xml, encoding="utf-8")
    print(f"Done: {out_path}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
