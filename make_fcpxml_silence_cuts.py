#!/usr/bin/env python3
"""
make_fcpxml_silence_cuts.py

Берёт видеофайл, находит паузы (ffmpeg silencedetect) и генерирует FCPXML,
где таймлайн состоит из кусочков без пауз. Все тайминги привязаны к границам кадров.

Требуется FFmpeg (ffmpeg и ffprobe в PATH).

Пример:
  python make_fcpxml_silence_cuts.py input.mp4 -o cuts.fcpxml \
      --noise -35 --min-silence 0.6 --min-clip 0.3 --pad 0.1
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
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

def frames_from_seconds(t: float, fps_num: int, fps_den: int) -> int:
    """Округляем время до целого числа кадров."""
    return int(round(t * fps_num / fps_den))

def seconds_rational_from_frames(frames: int, fps_num: int, fps_den: int) -> Tuple[int, int]:
    """Вернём (num, den) для формата 'num/dens' (= frames * (fps_den/fps_num))."""
    num = frames * fps_den
    den = fps_num
    return num, den

def fmt_rational_seconds(num: int, den: int) -> str:
    return f"{num}/{den}s"

def fmt_frames_as_seconds(frames: int, fps_num: int, fps_den: int) -> str:
    num, den = seconds_rational_from_frames(frames, fps_num, fps_den)
    return fmt_rational_seconds(num, den)

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

    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-print_format", "json", str(path)]
    p = run(cmd)
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

    return MediaInfo(src_url=src_url, duration=duration, width=v_width, height=v_height,
                     fps_num=fps_n, fps_den=fps_d, has_audio=has_audio)

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

def detect_silences(path: Path, noise_db: float, min_silence: float, *, total_dur: float = 0.0, show_progress: bool = True) -> List[Interval]:
    if not which("ffmpeg"):
        fail("ffmpeg не найден. Установите FFmpeg и убедитесь, что ffmpeg в PATH.")

    filt = f"silencedetect=noise={noise_db}dB:d={min_silence}"
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
                try:
                    current_start = float(m1.group(1))
                except ValueError:
                    current_start = None
                continue
            m2 = SILENCE_END_RE.search(line)
            if m2 and current_start is not None:
                try:
                    end = float(m2.group(1))
                except ValueError:
                    continue
                silences.append(Interval(current_start, end))
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

def pad_and_filter(intervals: List[Interval], total: float, pad: float, min_clip: float) -> List[Interval]:
    if not intervals:
        return []
    padded = []
    for iv in intervals:
        start = max(0.0, iv.start - pad)
        end = min(total, iv.end + pad)
        if end > start:
            padded.append(Interval(start, end))
    padded = merge_overlaps(padded)
    return [iv for iv in padded if iv.dur >= min_clip]

# ---------- FCPXML (кадровая квантовка) ----------

def build_fcpxml(mi: MediaInfo, clips: List[Interval], project_name: str) -> str:
    # Переводим все значения в ЦЕЛЫЕ КАДРЫ
    start_frames: List[int] = []
    dur_frames: List[int] = []
    for c in clips:
        sf = frames_from_seconds(c.start, mi.fps_num, mi.fps_den)
        df = frames_from_seconds(c.dur,   mi.fps_num, mi.fps_den)
        if df <= 0:
            continue
        start_frames.append(sf)
        dur_frames.append(df)

    # Накопительный offset — в кадрах
    offsets: List[int] = []
    acc = 0
    for df in dur_frames:
        offsets.append(acc)
        acc += df

    timeline_frames = acc

    # 1 кадр в рациональных секундах (напр., 1001/30000s)
    frame_duration_str = fmt_frames_as_seconds(1, mi.fps_num, mi.fps_den)

    # Элементы spine
    spine_items = []
    for i, (sf, of, df) in enumerate(zip(start_frames, offsets, dur_frames), 1):
        spine_items.append(
            f'            <asset-clip name="Segment {i}" ref="r1" '
            f'start="{fmt_frames_as_seconds(sf, mi.fps_num, mi.fps_den)}" '
            f'offset="{fmt_frames_as_seconds(of, mi.fps_num, mi.fps_den)}" '
            f'duration="{fmt_frames_as_seconds(df, mi.fps_num, mi.fps_den)}"/>'
        )
    spine_xml = "\n".join(spine_items) if spine_items else "            <!-- Нет клипов -->"

    # Цветовое пространство: Rec.709 (универсально для SDR)
    # Порядок полей: progressive (актуально для прогрессивного видео)
    # Квадратные пиксели: 1
    # Примечание: такие значения встречаются в рабочих примерах FCPXML (см. формат с colorSpace в примерах сообщества). 
    color_space = '1-1-1 (Rec. 709)'
    field_order = 'progressive'
    pixel_aspect = '1'

    asset_duration_frames = frames_from_seconds(mi.duration, mi.fps_num, mi.fps_den)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.10">
  <resources>
    <format id="r0" frameDuration="{frame_duration_str}" width="{mi.width}" height="{mi.height}"
            colorSpace="{color_space}" fieldOrder="{field_order}" pixelAspectRatio="{pixel_aspect}"/>
    <asset id="r1" name="source" start="0s"
           duration="{fmt_frames_as_seconds(asset_duration_frames, mi.fps_num, mi.fps_den)}"
           hasVideo="1" hasAudio="{1 if mi.has_audio else 0}" format="r0">
      <media-rep kind="original-media" src="{mi.src_url}"/>
    </asset>
  </resources>
  <library>
    <event name="{xml_escape(project_name)}">
      <project name="{xml_escape(project_name)}">
        <sequence format="r0"
                  duration="{fmt_frames_as_seconds(timeline_frames, mi.fps_num, mi.fps_den)}"
                  tcStart="0s" tcFormat="NDF">
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

def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )

# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Генерация FCPXML с нарезкой по паузам аудио (кадровые границы)")
    ap.add_argument("input", type=Path, help="Путь к видеофайлу")
    ap.add_argument("-o", "--out", type=Path, default=None, help="Путь к выходному .fcpxml (по умолчанию: рядом с видео)")
    ap.add_argument("--name", default=None, help="Имя проекта в FCP (по умолчанию: имя файла)")
    ap.add_argument("--noise", type=float, default=-35.0, help="Порог тишины в dBFS")
    ap.add_argument("--min-silence", type=float, default=0.6, help="Мин. длительность тишины, сек")
    ap.add_argument("--min-clip", type=float, default=0.3, help="Мин. длительность клипа, сек")
    ap.add_argument("--pad", type=float, default=0.05, help="Паддинг к каждому клипу, сек")
    ap.add_argument("--debug", action="store_true", help="Печатать найденные паузы и клипы")
    ap.add_argument("--no-progress", action="store_true", help="Отключить прогресс-бар")

    args = ap.parse_args()

    if not args.input.exists():
        fail(f"Файл не найден: {args.input}")

    mi = get_media_info(args.input)

    if not mi.has_audio:
        print("Предупреждение: у файла нет аудио-дорожки. Будет создан один клип на всю длительность.")
        clips = [Interval(0.0, mi.duration)]
        silences: List[Interval] = []
    else:
        silences = detect_silences(args.input, noise_db=args.noise, min_silence=args.min_silence, total_dur=mi.duration, show_progress=not args.no_progress)
        speech = invert_intervals(silences, mi.duration)
        clips = pad_and_filter(speech, mi.duration, pad=args.pad, min_clip=args.min_clip)

    if args.debug and mi.has_audio:
        print("\nНайденные паузы:")
        for iv in silences:
            print(f"  {iv.start:.3f} — {iv.end:.3f} ({iv.dur:.3f}s)")
        print("\nКлипы:")
        for i, iv in enumerate(clips, 1):
            print(f"  {i:02d}: {iv.start:.3f} — {iv.end:.3f} (dur {iv.dur:.3f}s)")

    name = args.name or args.input.stem
    xml = build_fcpxml(mi, clips, project_name=name)

    out_path = args.out or args.input.with_suffix(".fcpxml")
    out_path.write_text(xml, encoding="utf-8")
    print(f"Готово: {out_path}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)