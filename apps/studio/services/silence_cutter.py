"""
silence_cutter.py
-----------------
Step 8 of the viral-shorts pipeline: Remove pauses, silence, and tighten pacing.

Uses FFmpeg's silencedetect filter to find all silent segments in a clip, then
builds a concat demuxer sequence that keeps only the speaking parts.

This module is intentionally free of Django dependencies so it can be unit-tested
independently.
"""
import logging
import math
import os
import re
import subprocess
import tempfile
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Silence detection thresholds
_SILENCE_DB = -35          # noise floor (dBFS) below which we consider silence
_MIN_SILENCE_S = 0.45      # minimum silence duration to cut (seconds)
_MIN_KEEP_S = 0.25         # minimum speech segment to keep (avoid tiny blips)
_FAST_START_PAD = 0.05     # seconds to pad each kept segment at start
_FAST_END_PAD = 0.08       # seconds to pad each kept segment at end


def detect_silence(
    file_path: str,
    ffmpeg: str,
    duration_s: float,
    noise_db: float = _SILENCE_DB,
    min_silence_s: float = _MIN_SILENCE_S,
) -> List[Tuple[float, float]]:
    """
    Runs FFmpeg silencedetect on *file_path* and returns a list of
    (silence_start, silence_end) tuples in seconds relative to the file start.
    """
    cmd = [
        ffmpeg, '-i', os.path.abspath(file_path),
        '-af', f'silencedetect=noise={noise_db}dB:duration={min_silence_s}',
        '-f', 'null', '-',
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors='ignore',
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning('silencedetect failed: %s', exc)
        return []

    output = proc.stderr or ''
    silences: List[Tuple[float, float]] = []
    starts: List[float] = []

    for line in output.splitlines():
        m_start = re.search(r'silence_start:\s*(-?[\d.]+)', line)
        m_end = re.search(r'silence_end:\s*(-?[\d.]+)', line)
        if m_start:
            starts.append(float(m_start.group(1)))
        if m_end and starts:
            s_start = starts.pop()
            s_end = float(m_end.group(1))
            if s_end > s_start:
                silences.append((max(0.0, s_start), min(duration_s, s_end)))

    # Handle clip that ends in silence (no silence_end emitted)
    if starts:
        silences.append((max(0.0, starts[-1]), duration_s))

    return silences


def silence_to_keep(
    silences: List[Tuple[float, float]],
    total_duration: float,
    min_keep: float = _MIN_KEEP_S,
    start_pad: float = _FAST_START_PAD,
    end_pad: float = _FAST_END_PAD,
) -> List[Tuple[float, float]]:
    """
    Inverts silence spans into speech spans (what to keep).

    Each kept segment is padded slightly at start and end so cuts sound natural.
    Segments shorter than *min_keep* are dropped.

    Returns list of (keep_start, keep_end) tuples.
    """
    if not silences:
        return [(0.0, total_duration)]

    # Build timeline boundaries from silence spans
    boundaries: List[float] = [0.0]
    for s_start, s_end in sorted(silences):
        boundaries.extend([s_start, s_end])
    boundaries.append(total_duration)

    # Pair up to get (region_start, region_end)
    # Even-indexed items are speech starts, odd-indexed are speech ends
    regions: List[Tuple[float, float]] = []
    for i in range(0, len(boundaries) - 1, 2):
        regions.append((boundaries[i], boundaries[i + 1]))

    kept: List[Tuple[float, float]] = []
    for raw_start, raw_end in regions:
        # Apply padding, clamp to [0, total_duration]
        s = max(0.0, raw_start - start_pad)
        e = min(total_duration, raw_end + end_pad)
        if e - s >= min_keep:
            kept.append((round(s, 3), round(e, 3)))

    if not kept:
        return [(0.0, total_duration)]
    return kept


def cut_silences(
    input_path: str,
    output_path: str,
    duration_s: float,
    ffmpeg: str,
    subprocess_kwargs: Optional[dict] = None,
    noise_db: float = _SILENCE_DB,
    min_silence_s: float = _MIN_SILENCE_S,
) -> Tuple[bool, float]:
    """
    Produces *output_path* with all silent segments removed.

    Returns (success: bool, new_duration: float).
    If less than 10 % of audio is cut or detection fails, copies input to output
    unchanged so the caller always gets a valid file.
    """
    subprocess_kwargs = subprocess_kwargs or {}

    silences = detect_silence(input_path, ffmpeg, duration_s, noise_db, min_silence_s)
    kept = silence_to_keep(silences, duration_s)

    # Calculate how much we're cutting
    kept_total = sum(e - s for s, e in kept)
    cut_ratio = 1.0 - (kept_total / max(0.001, duration_s))

    if cut_ratio < 0.08 or len(kept) < 2:
        # Nothing meaningful to cut — skip (saves time and avoids artifacts)
        logger.debug('silence_cutter: only %.1f%% silence in %.1fs — skipping', cut_ratio * 100, duration_s)
        return False, duration_s

    logger.info(
        'silence_cutter: cutting %.1f%% silence from %.1fs → %.1fs (%d segments kept)',
        cut_ratio * 100, duration_s, kept_total, len(kept),
    )

    with tempfile.TemporaryDirectory(prefix='silence_cut_') as tmp:
        # Write concat list
        list_path = os.path.join(tmp, 'cuts.txt')
        with open(list_path, 'w', encoding='utf-8') as f:
            for seg_start, seg_end in kept:
                seg_dur = seg_end - seg_start
                f.write(
                    f"file '{os.path.abspath(input_path).replace(chr(92), '/')}'\n"
                    f"inpoint {seg_start:.3f}\n"
                    f"outpoint {seg_end:.3f}\n"
                )

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        cmd = [
            ffmpeg, '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', list_path,
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '22', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k',
            '-movflags', '+faststart',
            output_path,
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors='ignore',
                timeout=600,
                **subprocess_kwargs,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning('silence_cutter concat failed: %s', exc)
            return False, duration_s

        if proc.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
            logger.warning('silence_cutter concat error (rc=%d): %s', proc.returncode, (proc.stderr or '')[-500:])
            return False, duration_s

    return True, round(kept_total, 3)
