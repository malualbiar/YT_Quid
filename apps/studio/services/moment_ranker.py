import math


SCORE_FACTORS = (
    'hook_strength',
    'emotional_intensity',
    'surprise',
    'humor',
    'payoff',
    'standalone_context',
    'audio_clarity',
    'visual_interest',
    'duration_suitability',
)


def validate_moment(moment, chunk_start, chunk_duration, video_duration):
    if not isinstance(moment, dict):
        return None
    try:
        start = float(moment['start']) + chunk_start
        end = float(moment['end']) + chunk_start
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        return None
    if start < chunk_start - 0.5 or end > chunk_start + chunk_duration + 0.5:
        return None
    start = max(0.0, start)
    end = min(video_duration, end)
    if end <= start:
        return None

    components = moment.get('score_components')
    if not isinstance(components, dict):
        return None
    try:
        scores = {factor: int(components[factor]) for factor in SCORE_FACTORS}
    except (KeyError, TypeError, ValueError):
        return None
    if any(score < 1 or score > 10 for score in scores.values()):
        return None

    title = str(moment.get('title', '')).strip()[:180]
    if not title:
        return None
    try:
        suggested_duration = float(moment.get('suggested_duration', end - start))
    except (TypeError, ValueError):
        suggested_duration = end - start
    if not math.isfinite(suggested_duration) or suggested_duration <= 0:
        suggested_duration = end - start

    return {
        'start_seconds': round(start, 2),
        'end_seconds': round(end, 2),
        'title': title,
        'description': str(moment.get('description', '')).strip()[:1200],
        'category': str(moment.get('category', 'other')).strip().lower()[:60] or 'other',
        'reason': str(moment.get('reason', '')).strip()[:1200],
        'suggested_duration': round(suggested_duration, 2),
        'score': round(sum(scores.values()) * 100 / (len(scores) * 10)),
        'score_components': scores,
        'needs_context': bool(moment.get('needs_context', False)),
    }


def rank_and_deduplicate(moments, minimum_gap_seconds=15, maximum=12):
    ranked = sorted(moments, key=lambda item: (-item['score'], item['start_seconds']))
    accepted = []
    gap = max(0.0, float(minimum_gap_seconds))
    for candidate in ranked:
        overlaps = any(
            candidate['start_seconds'] < existing['end_seconds'] + gap
            and candidate['end_seconds'] > existing['start_seconds'] - gap
            for existing in accepted
        )
        if not overlaps:
            accepted.append(candidate)
        if len(accepted) >= max(1, int(maximum)):
            break
    return sorted(accepted, key=lambda item: (item['start_seconds'], item['end_seconds']))