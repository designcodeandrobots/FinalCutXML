#!/usr/bin/env python3
"""
make_fcpxml_silence_cuts.py

• Находит паузы (ffmpeg silencedetect) и собирает таймлайн из отрезков без пауз.
• Все тайминги квантуются по границам кадров и сериализуются как рациональные секунды.
• Порог шума — вручную (--noise) или автоматически (--auto-noise).
• Защита краёв слов:
  - Округление наружу: start=floor, end=ceil
  - Асимметричный паддинг клипов: --pad-pre / --pad-post
  - Укорачивание тишины перед инверсией: --shrink-silence-pre / --shrink-silence-post
• (опц.) Предфильтр для детектора: --prefilter "highpass=f=120,lowpass=f=8000"

Пример (1080p30, короткие паузы 0.5с, защита краёв):
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
    """Округлить время вниз до ближайшего кадра (без усечения начала)."""
    return int(math.floor((t * fps_num / fps_den) + 1e-9))

def frames_ceil(t: float, fps_num: int, fps_den: int) -> int:
    """Округлить время вверх до ближайшего кадра (не усекать конец)."""
    return int(math.ceil((t * fps_num / fps_den) - 1e-9))

def seconds_rational_from_frames(frames: int, fps_num: int, fps_den: int) -> Tuple[int, int]:
    return frames * fps_den, fps_num

def fmt_frames_as_seconds(frames: int, fps_num: int, fps_den: int) -> str:
    num, den = seconds_rational_from_frames(frames, fps_num, fps_den)
    return f"{num}/{den}s"

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
        fail("ffprobe не найден. Установите FFmpeg и убедитесь, что ffprobe в PATH.")
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

def _print_progress(cur: float, total: float, label: str = "Анализ"):
    import shutil
    width = max(20, shutil.get_terminal_size((80, 20)).columns - 20)
    pct = 0.0 if total <= 0 else min(1.0, max(0.0, cur / total))
    filled = int(pct * width)
    bar = "█" * filled + "·" * (width - filled)
    sys.stdout.write(f"\r{label} [{bar}] {pct*100:5.1f}%")
    sys.stdout.flush()

def detect_silences(path: Path, noise_db: float, min_silence: float, *,
                    total_dur: float = 0.0, show_progress: bool = True, prefilter: str = "") -> List[Interval]:
    if not which("ffmpeg"):
        fail("ffmpeg не найден. Установите FFmpeg и убедитесь, что ffmpeg в PATH.")
    chain = []
    if prefilter.strip():
        chain.append(prefilter.strip())
    chain.append(f"silencedetect=noise={noise_db}dB:d={min_silence}")
    filt = ",".join(chain)
    cmd = ["ffmpeg", "-hide_banner", "-i", str(path), "-af", filt, "-f", "null", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    silences: List[Interval] = []
    current_start: Optional[float] = None
    last_time = 0.0
    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            t = TIME_RE.search(line)
            if t:
                last_time = _hms_to_seconds(*t.groups())
                if show_progress:
                    _print_progress(last_time, total_dur or last_time)
            m1 = SILENCE_START_RE.search(line)
            if m1:
                current_start = float(m1.group(1))
                continue
            m2 = SILENCE_END_RE.search(line)
            if m2 and current_start is not None:
                silences.append(Interval(current_start, float(m2.group(1))))
                current_start = None
    finally:
        proc.wait()
        if show_progress:
            _print_progress(total_dur or last_time, total_dur or last_time)
            sys.stdout.write("\n")
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
    """Уменьшить каждую тишину: сдвинуть её начало вперёд на shrink_pre и конец назад на shrink_post."""
    if not intervals:
        return []
    out: List[Interval] = []
    for iv in intervals:
        s = min(max(0.0, iv.start + max(0.0, shrink_pre)), total)
        e = min(max(0.0, iv.end   - max(0.0, shrink_post)), total)
        if e - s > 1e-6:
            out.append(Interval(s, e))
        # если тишина "схлопнулась" — просто пропускаем её (т.е. рассматриваем как отсутствие тишины)
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
    """Асимметричный паддинг: pre — до начала, post — после конца."""
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

# ---------- Автооценка шумового пола ----------

def estimate_noise_floor_db(path: Path, sample_dur: float = 30.0) -> Optional[float]:
    """Оценить mean_volume (дБFS) на первых sample_dur сек."""
    if not which("ffmpeg"):
        return None
    cmd = ["ffmpeg", "-hide_banner", "-i", str(path), "-t", str(sample_dur),
           "-af", "volumedetect", "-f", "null", "-"]
    p = run(cmd)
    m = re.search(r"mean_volume:\s*(-?\d+(\.\d+)?) dB", p.stderr)
    if not m:
        return None
    return float(m.group(1))

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
    # Пересчитываем клипы в кадры с округлением наружу
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

    # Накопительные offset'ы
    offsets: List[int] = []
    acc = 0
    for df in dur_frames:
        offsets.append(acc)
        acc += df
    timeline_frames = acc

    frame_duration_str = fmt_frames_as_seconds(1, timeline_fps_num, timeline_fps_den)

    # Сборка spine
    spine_items: List[str] = []
    for i, (sf, of, df) in enumerate(zip(start_frames, offsets, dur_frames), 1):
        spine_items.append(
            f'            <asset-clip name="Segment {i}" ref="r1" '
            f'start="{fmt_frames_as_seconds(sf, timeline_fps_num, timeline_fps_den)}" '
            f'offset="{fmt_frames_as_seconds(of, timeline_fps_num, timeline_fps_den)}" '
            f'duration="{fmt_frames_as_seconds(df, timeline_fps_num, timeline_fps_den)}"/>'
        )
    spine_xml = "\n".join(spine_items) if spine_items else "            <!-- Нет клипов -->"

    # Ресурсы и секвенция
    asset_duration_frames = frames_ceil(mi.duration, timeline_fps_num, timeline_fps_den)
    format_line = (
        f'    <format id="r0" frameDuration="{frame_duration_str}" '
        f'width="{timeline_width}" height="{timeline_height}"/>'
    )
    sequence_open = (
        f'        <sequence format="r0" '
        f'duration="{fmt_frames_as_seconds(timeline_frames, timeline_fps_num, timeline_fps_den)}" '
        f'tcStart="0s" tcFormat="NDF">'
    )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.10">
  <resources>
{format_line}
    <asset id="r1" name="source" start="0s"
           duration="{fmt_frames_as_seconds(asset_duration_frames, timeline_fps_num, timeline_fps_den)}"
           hasVideo="1" hasAudio="{1 if mi.has_audio else 0}" format="r0">
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
    ap = argparse.ArgumentParser(description="FCPXML: разрезка по паузам (с защитой краёв)")
    ap.add_argument("input", type=Path, help="Путь к видеофайлу")
    ap.add_argument("-o", "--out", type=Path, default=None, help="Куда сохранять .fcpxml (по умолчанию рядом с видео)")
    ap.add_argument("--name", default=None, help="Имя проекта (по умолчанию — имя файла)")

    # Детектор тишины
    ap.add_argument("--noise", type=float, default=-35.0, help="Порог тишины в dBFS (если не включён auto-noise)")
    ap.add_argument("--auto-noise", dest="auto_noise", action="store_true", help="Автооценка порога тишины")
    ap.add_argument("--auto-noise-margin", dest="auto_noise_margin", type=float, default=5.0,
                    help="Запас (dB), который добавляется к шумовому полу при auto-noise")
    ap.add_argument("--min-silence", type=float, default=0.6, help="Мин. длительность тишины, сек")

    # Предфильтр для детектора (необязательно)
    ap.add_argument("--prefilter", default="", help="FFmpeg-фильтры перед silencedetect (напр. 'highpass=f=120,lowpass=f=8000')")

    # Постобработка речи
    ap.add_argument("--min-clip", type=float, default=0.3, help="Мин. длительность клипа, сек")
    ap.add_argument("--pad", type=float, default=0.05, help="Базовый паддинг до/после клипа, сек")
    ap.add_argument("--pad-pre", type=float, default=None, help="Паддинг ДО клипа (перекрывает --pad)")
    ap.add_argument("--pad-post", type=float, default=None, help="Паддинг ПОСЛЕ клипа (перекрывает --pad)")
    ap.add_argument("--shrink-silence-pre", type=float, default=0.0, help="Укоротить начало каждой тишины на N сек")
    ap.add_argument("--shrink-silence-post", type=float, default=0.0, help="Укоротить конец каждой тишины на N сек")

    ap.add_argument("--debug", action="store_true", help="Печатать найденные паузы/клипы")
    ap.add_argument("--no-progress", action="store_true", help="Отключить прогресс-бар")

    # Формат таймлайна
    ap.add_argument("--format-width",  type=int, default=None, help="Ширина таймлайна (например, 1920)")
    ap.add_argument("--format-height", type=int, default=None, help="Высота таймлайна (например, 1080)")
    ap.add_argument("--format-fps-num", type=int, default=None, help="FPS числитель (например, 30)")
    ap.add_argument("--format-fps-den", type=int, default=None, help="FPS знаменатель (например, 1)")

    args = ap.parse_args()

    if not args.input.exists():
        fail(f"Файл не найден: {args.input}")

    mi = get_media_info(args.input)

    # автоопределение шума
    if args.auto_noise and mi.has_audio:
        est = estimate_noise_floor_db(args.input)
        if est is not None:
            args.noise = est + args.auto_noise_margin
            print(f"[auto-noise] шумовой пол ≈ {est:.1f} dBFS → порог тишины --noise {args.noise:.1f} dB")

    # 1) Детектим тишины (с возможным предфильтром)
    if not mi.has_audio:
        print("Предупреждение: у файла нет аудио-дорожки. Будет один клип на всю длительность.")
        clips = [Interval(0.0, mi.duration)]
        silences: List[Interval] = []
    else:
        silences = detect_silences(
            args.input,
            noise_db=args.noise,
            min_silence=args.min_silence,
            total_dur=mi.duration,
            show_progress=not args.no_progress,
            prefilter=args.prefilter
        )

        # 2) Укорачиваем тишины → расширяем речь
        silences = shrink_silences(
            silences, mi.duration,
            shrink_pre=args.shrink_silence_pre,
            shrink_post=args.shrink_silence_post
        )

        # 3) Инвертируем в речь и добавляем паддинг
        speech = invert_intervals(silences, mi.duration)
        clips = pad_and_filter(
            speech, mi.duration,
            pad=args.pad, min_clip=args.min_clip,
            pad_pre=args.pad_pre, pad_post=args.pad_post
        )

    if args.debug and mi.has_audio:
        print("\nТишины (после shrink):")
        for iv in silences:
            print(f"  S: {iv.start:.3f} — {iv.end:.3f} ({iv.dur:.3f}s)")
        print("\nКлипы:")
        for i, iv in enumerate(clips, 1):
            print(f"  {i:02d}: {iv.start:.3f} — {iv.end:.3f} (dur {iv.dur:.3f}s)")

    # Таймлайн-формат
    timeline_width  = args.format_width  or mi.width
    timeline_height = args.format_height or mi.height
    timeline_fps_n  = args.format_fps_num or mi.fps_num
    timeline_fps_d  = args.format_fps_den or mi.fps_den

    # XML
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
    print(f"Готово: {out_path}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)