#!/usr/bin/env python3
"""
make_fcpxml_silence_cuts.py

1) Находит паузы (ffmpeg silencedetect) и собирает таймлайн из отрезков без пауз.
2) (опц.) Распознаёт речь (faster-whisper) и добавляет текст как маркеры внутри клипов.
3) Все тайминги квантуются по границам кадров и сериализуются как рациональные секунды.
4) Формат таймлайна явно задаётся <sequence format="r0">; при желании его можно указать флагами.

Зависимости:
  - FFmpeg: ffmpeg, ffprobe в PATH
  - (опц.) faster-whisper: pip install faster-whisper

Примеры:
  # Авто (берём параметры из исходника)
  python3 make_fcpxml_silence_cuts.py input.mp4 -o cuts.fcpxml

  # Жёстко задать 1080p25
  python3 make_fcpxml_silence_cuts.py input.mp4 -o cuts.fcpxml \
    --format-width 1920 --format-height 1080 --format-fps-num 25 --format-fps-den 1

  # 1080p29.97 + STT (рус)
  python3 make_fcpxml_silence_cuts.py input.mp4 -o cuts.fcpxml \
    --format-width 1920 --format-height 1080 --format-fps-num 30000 --format-fps-den 1001 \
    --stt --stt-model small --language ru
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
    """Округление времени до ближайшего кадра."""
    return int(round(t * fps_num / fps_den))

def seconds_rational_from_frames(frames: int, fps_num: int, fps_den: int) -> Tuple[int, int]:
    """Вернём (num, den) для 'num/dens' (= frames * (fps_den/fps_num))."""
    return frames * fps_den, fps_num

def fmt_frames_as_seconds(frames: int, fps_num: int, fps_den: int) -> str:
    num, den = seconds_rational_from_frames(frames, fps_num, fps_den)
    return f"{num}/{den}s"

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
        end   = min(total, iv.end + pad)
        if end > start:
            padded.append(Interval(start, end))
    padded = merge_overlaps(padded)
    return [iv for iv in padded if iv.dur >= min_clip]

# ---------- STT (faster-whisper) ----------

@dataclass
class STTSegment:
    start: float
    end: float
    text: str

def transcribe_audio(path: Path, model_name: str, language: Optional[str], merge_gap: float, max_chars: int, show_progress: bool) -> List[STTSegment]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        fail("Не установлен faster-whisper. Установите: pip3 install faster-whisper")
    model = WhisperModel(model_name, device="auto", compute_type="auto")
    seg_iter, _info = model.transcribe(str(path), language=language, vad_filter=True, beam_size=1)
    raw: List[STTSegment] = [STTSegment(float(s.start), float(s.end), s.text.strip()) for s in seg_iter]

    # Схлопываем короткие паузы между фразами
    merged: List[STTSegment] = []
    for seg in raw:
        if not seg.text:
            continue
        if merged and seg.start - merged[-1].end <= max(0.0, merge_gap):
            merged[-1].end = seg.end
            merged[-1].text = (merged[-1].text + " " + seg.text).strip()
        else:
            merged.append(STTSegment(seg.start, seg.end, seg.text))

    # Ограничим длину текста
    out: List[STTSegment] = []
    for s in merged:
        t = " ".join(s.text.split())
        if max_chars > 0 and len(t) > max_chars:
            t = t[:max_chars].rstrip() + "…"
        out.append(STTSegment(s.start, s.end, t))
    if show_progress:
        print(f"STT: сегментов после объединения: {len(out)}")
    return out

def stt_segments_to_markers_per_clip(
    clips: List[Interval],
    stt: List[STTSegment],
    fps_num: int,
    fps_den: int,
    per_clip_strategy: str = "segments"  # "segments" | "one"
) -> List[List[Tuple[int, str]]]:
    """Для каждого клипа вернуть список (frame_offset, text)."""
    out: List[List[Tuple[int, str]]] = []
    for c in clips:
        items: List[Tuple[int, str]] = []
        if per_clip_strategy == "one":
            texts = [s.text for s in stt if s.start < c.end and s.end > c.start]
            if texts:
                items.append((0, " ".join(texts)))
        else:
            for s in stt:
                if s.end <= c.start or s.start >= c.end:
                    continue
                rel_start = max(0.0, s.start - c.start)
                f = frames_from_seconds(rel_start, fps_num, fps_den)
                items.append((f, s.text))
        out.append(items)
    return out

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
    markers_by_clip: Optional[List[List[Tuple[int, str]]]] = None,
    *,
    timeline_width: int,
    timeline_height: int,
    timeline_fps_num: int,
    timeline_fps_den: int,
) -> str:
    # Переводим клипы в кадры по таймлайну
    start_frames: List[int] = []
    dur_frames: List[int] = []
    for c in clips:
        sf = frames_from_seconds(c.start, timeline_fps_num, timeline_fps_den)
        df = frames_from_seconds(c.dur,   timeline_fps_num, timeline_fps_den)
        if df <= 0:
            continue
        start_frames.append(sf)
        dur_frames.append(df)

    # offsets (накопительные, в кадрах)
    offsets: List[int] = []
    acc = 0
    for df in dur_frames:
        offsets.append(acc)
        acc += df
    timeline_frames = acc

    frame_duration_str = fmt_frames_as_seconds(1, timeline_fps_num, timeline_fps_den)

    # Сборка spine
    spine_items = []
    for i, (sf, of, df) in enumerate(zip(start_frames, offsets, dur_frames), 1):
        # Маркеры внутри клипа
        marker_xml = ""
        if markers_by_clip and i-1 < len(markers_by_clip):
            mk_lines = []
            one_frame = fmt_frames_as_seconds(1, timeline_fps_num, timeline_fps_den)
            for m_off_frames, m_text in markers_by_clip[i-1]:
                mk_lines.append(
                    f'              <marker start="{fmt_frames_as_seconds(m_off_frames, timeline_fps_num, timeline_fps_den)}" duration="{one_frame}" value="{xml_escape(m_text)}"/>'
                )
            marker_xml = ("\n" + "\n".join(mk_lines) + "\n            ") if mk_lines else ""

        spine_items.append(
            f'            <asset-clip name="Segment {i}" ref="r1" '
            f'start="{fmt_frames_as_seconds(sf, timeline_fps_num, timeline_fps_den)}" '
            f'offset="{fmt_frames_as_seconds(of, timeline_fps_num, timeline_fps_den)}" '
            f'duration="{fmt_frames_as_seconds(df, timeline_fps_num, timeline_fps_den)}">{marker_xml}</asset-clip>'
        )
    spine_xml = "\n".join(spine_items) if spine_items else "            <!-- Нет клипов -->"

    asset_duration_frames = frames_from_seconds(mi.duration, timeline_fps_num, timeline_fps_den)

    # Минимальный и совместимый формат проекта
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
    ap = argparse.ArgumentParser(description="FCPXML: разрезка по паузам + (опц.) маркеры речи")
    ap.add_argument("input", type=Path, help="Путь к видеофайлу")
    ap.add_argument("-o", "--out", type=Path, default=None, help="Куда сохранять .fcpxml (по умолчанию рядом с видео)")
    ap.add_argument("--name", default=None, help="Имя проекта (по умолчанию — имя файла)")

    # Детектор тишины
    ap.add_argument("--noise", type=float, default=-35.0, help="Порог тишины в dBFS")
    ap.add_argument("--min-silence", type=float, default=0.6, help="Мин. длительность тишины, сек")
    ap.add_argument("--min-clip", type=float, default=0.3, help="Мин. длительность клипа, сек")
    ap.add_argument("--pad", type=float, default=0.05, help="Паддинг к каждому клипу, сек")

    ap.add_argument("--debug", action="store_true", help="Печатать найденные паузы/клипы")
    ap.add_argument("--no-progress", action="store_true", help="Отключить прогресс-бар")

    # STT
    ap.add_argument("--stt", action="store_true", help="Включить распознавание речи и маркеры текста")
    ap.add_argument("--stt-model", default="small", help="Модель faster-whisper (tiny/base/small/medium/large-v3 …)")
    ap.add_argument("--language", default=None, help="Язык (например, 'ru', 'en'); по умолчанию авто")
    ap.add_argument("--stt-merge-gap", type=float, default=0.40, help="Схлопывать фразы, если пауза короче, сек")
    ap.add_argument("--stt-max-chars", type=int, default=90, help="Ограничение длины текста в маркере (0 — без лимита)")
    ap.add_argument("--stt-per-clip", choices=["segments","one"], default="segments",
                    help="Маркеры: 'segments' — по каждой фразе; 'one' — один маркер на клип")

    # Формат таймлайна (перекрывает авто-детект с исходника)
    ap.add_argument("--format-width",  type=int, default=None, help="Ширина таймлайна (например, 1920)")
    ap.add_argument("--format-height", type=int, default=None, help="Высота таймлайна (например, 1080)")
    ap.add_argument("--format-fps-num", type=int, default=None, help="FPS числитель (например, 30000)")
    ap.add_argument("--format-fps-den", type=int, default=None, help="FPS знаменатель (например, 1001)")

    args = ap.parse_args()

    if not args.input.exists():
        fail(f"Файл не найден: {args.input}")

    mi = get_media_info(args.input)

    # 1) Режем по тишине
    if not mi.has_audio:
        print("Предупреждение: у файла нет аудио-дорожки. Будет один клип на всю длительность.")
        clips = [Interval(0.0, mi.duration)]
        silences: List[Interval] = []
    else:
        silences = detect_silences(args.input, noise_db=args.noise, min_silence=args.min_silence,
                                   total_dur=mi.duration, show_progress=not args.no_progress)
        speech = invert_intervals(silences, mi.duration)
        clips = pad_and_filter(speech, mi.duration, pad=args.pad, min_clip=args.min_clip)

    if args.debug and mi.has_audio:
        print("\nНайденные паузы:")
        for iv in silences:
            print(f"  {iv.start:.3f} — {iv.end:.3f} ({iv.dur:.3f}s)")
        print("\nКлипы:")
        for i, iv in enumerate(clips, 1):
            print(f"  {i:02d}: {iv.start:.3f} — {iv.end:.3f} (dur {iv.dur:.3f}s)")

    # 2) (опц.) STT -> маркеры
    markers_by_clip: Optional[List[List[Tuple[int, str]]]] = None
    if args.stt and mi.has_audio:
        stt_segments = transcribe_audio(
            args.input,
            model_name=args.stt_model,
            language=args.language,
            merge_gap=args.stt_merge_gap,
            max_chars=args.stt_max_chars,
            show_progress=not args.no_progress
        )
        markers_by_clip = stt_segments_to_markers_per_clip(
            clips, stt_segments, mi.fps_num, mi.fps_den, per_clip_strategy=args.stt_per_clip
        )

    # 3) Таймлайн-формат: либо флаги, либо из исходника
    timeline_width  = args.format_width  or mi.width
    timeline_height = args.format_height or mi.height
    timeline_fps_n  = args.format_fps_num or mi.fps_num
    timeline_fps_d  = args.format_fps_den or mi.fps_den

    # 4) XML
    name = args.name or args.input.stem
    xml = build_fcpxml(
        mi, clips,
        project_name=name,
        markers_by_clip=markers_by_clip,
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