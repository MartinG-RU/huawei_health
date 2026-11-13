"""TCX for all recorded workouts. Never invent a route or HR timing."""
from __future__ import annotations
import json
from pathlib import Path
import xml.etree.ElementTree as ET
from . import gps_export as gps

MAPPING = json.loads(Path(__file__).with_name('sport_mapping.json').read_text(encoding='utf-8'))
NS = 'http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2'


def records(payload, depth=0):
    if depth > 10:
        return
    if isinstance(payload, dict):
        if all(k in payload for k in ('startTime', 'totalTime', 'sportType')):
            yield payload
        else:
            for value in payload.values():
                if isinstance(value, (dict, list)):
                    yield from records(value, depth + 1)
    elif isinstance(payload, list):
        for value in payload:
            if isinstance(value, (dict, list)):
                yield from records(value, depth + 1)


def sport(row):
    n = gps.numeric(row.get('sportType'))
    return MAPPING.get(str(int(n)) if n is not None and n.is_integer() else '', ['Workout', 'Workout'])


def bounds(row):
    start, end, duration = (gps.numeric(row.get(k)) for k in ('startTime', 'endTime', 'totalTime'))
    if (start is None or end is None or duration is None or
            not 946684800000 <= start < end <= 4102444800000 or
            not 0 < duration <= end - start + 60000):
        raise ValueError('invalid_workout_times')
    return start / 1000, end / 1000, duration / 1000


def route(row):
    try:
        return gps.points(row)
    except ValueError:
        return []


def heart_samples(row):
    start, end, _ = bounds(row)
    raw = []
    for line in gps.sequence(row).splitlines():
        fields = dict(token.split('=', 1) for token in line.split(';') if '=' in token)
        fields = {k.strip(): v.strip() for k, v in fields.items()}
        if fields.get('tp') != 'h-r':
            continue
        k, v = gps.numeric(fields.get('k')), gps.numeric(fields.get('v'))
        if k is not None and v is not None and v.is_integer() and 1 <= v <= 255:
            raw.append((k, int(v)))
    if not raw:
        return [], 'absent'
    # Absolute timestamps only. Relative offsets/indices are deliberately NOT guessed.
    candidates = []
    for divisor, label in ((1, 'epoch_seconds'), (1000, 'epoch_milliseconds')):
        samples = [(k / divisor, v) for k, v in raw]
        if all(start <= t <= end for t, _ in samples):
            candidates.append((samples, label))
    if len(candidates) != 1:
        return [], 'unverified_timebase'
    samples, label = candidates[0]
    unique = {}
    for t, v in samples:
        if t in unique and unique[t] != v:
            return [], 'conflicting_timestamps'
        unique[t] = v
    return sorted(unique.items()), label


def build_tcx(row):
    start, end, duration = bounds(row)
    kind, name = sport(row)
    points = route(row)
    hearts, timebase = heart_samples(row)
    distance = gps.numeric(row.get('totalDistance'))
    if distance is not None and distance < 0:
        raise ValueError('invalid_distance')
    ET.register_namespace('', NS)
    def child(parent, tag, text=None, **attrs):
        element = ET.SubElement(parent, '{' + NS + '}' + tag, attrs)
        if text is not None:
            element.text = str(text)
        return element
    root = ET.Element('{' + NS + '}TrainingCenterDatabase')
    activities = child(root, 'Activities')
    activity = child(activities, 'Activity', Sport='Running' if kind in ('Run', 'TrailRun') else 'Biking' if kind == 'Ride' else 'Other')
    child(activity, 'Id', gps.iso(start))
    lap = child(activity, 'Lap', StartTime=gps.iso(start))
    child(lap, 'TotalTimeSeconds', duration)
    child(lap, 'DistanceMeters', distance if distance is not None else 0)
    # TCX requires Calories. Zero is an unavailable sentinel, not a calorie estimate.
    child(lap, 'Calories', 0)
    child(lap, 'Intensity', 'Active')
    child(lap, 'TriggerMethod', 'Manual')
    events = {t: {'hr': v} for t, v in hearts}
    for point in points:
        events.setdefault(point['time'], {}).update(point)
    # Time-only endpoints support route-less workouts; no coordinates or distance curve invented.
    events.setdefault(start, {})
    events.setdefault(end, {})
    track = child(lap, 'Track')
    for t, event in sorted(events.items()):
        point = child(track, 'Trackpoint')
        child(point, 'Time', gps.iso(t))
        if 'lat' in event:
            position = child(point, 'Position')
            child(position, 'LatitudeDegrees', event['lat'])
            child(position, 'LongitudeDegrees', event['lon'])
            if event.get('ele') is not None:
                child(point, 'AltitudeMeters', event['ele'])
        if 'hr' in event:
            child(child(point, 'HeartRateBpm'), 'Value', event['hr'])
    child(activity, 'Notes', f'Huawei Health: {name}; source sportType={row.get("sportType")}. No interpolated route or HR. Missing distance/calories use required TCX zero sentinel.')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True), {
        'gps_points': len(points), 'altitude_points': sum(x['ele'] is not None for x in points),
        'heart_rate_points': len(hearts), 'heart_rate_timebase': timebase,
        'target_sport_type': kind, 'source_sport_type': gps.numeric(row.get('sportType'))}
