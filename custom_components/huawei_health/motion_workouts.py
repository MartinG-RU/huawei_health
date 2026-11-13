"""Read recorded MotionPath workouts, not reconstructed activity minutes.

Walking type 5 and duration milliseconds confirmed against this user's GPX.
Other type labels and calorie units are deliberately not guessed.
"""
from datetime import datetime
import math

PATH = '/dataQuery/path/getMotionPathData'


def number(v):
    if isinstance(v, bool): return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError): return None


def records(payload):
    if isinstance(payload, dict):
        if 'startTime' in payload and 'totalTime' in payload and 'totalDistance' in payload:
            yield payload
        else:
            for value in payload.values():
                if isinstance(value, (dict, list)):
                    yield from records(value)
    elif isinstance(payload, list):
        for value in payload:
            if isinstance(value, (dict, list)):
                yield from records(value)


def shape(responses):
    seen=set()
    out=[]
    for payload in responses:
        for row in records(payload):
            start=number(row.get('startTime')); end=number(row.get('endTime'))
            duration=number(row.get('totalTime')); distance=number(row.get('totalDistance'))
            if start is None or end is None or end<start or duration is None or duration<0 or distance is None or distance<0:
                continue
            # IDs stay in memory for deduplication, never in sensor attributes.
            record_id=row.get('recordId')
            key=('record',str(record_id)) if record_id else ('fallback',str(row.get('deviceCode')),start,end,str(row.get('sportType')),distance)
            if key in seen: continue
            try:
                a=datetime.fromtimestamp(start/1000); b=datetime.fromtimestamp(end/1000)
            except (ValueError, OverflowError, OSError): continue
            seen.add(key)
            kind=number(row.get('sportType'))
            kind=int(kind) if kind is not None and kind.is_integer() else None
            out.append({'start':a,'end':b,'duration_min':duration/60000,
                        'distance_m':distance,'steps':number(row.get('totalSteps')),
                        'kcal':None, # totalCalories unit not independently confirmed
                        'segments':1,'sport_type':kind,'device_code':row.get('deviceCode'),
                        'record_source':'motion_path','confirmed_label':'walking' if kind==5 else 'unknown',
                        'duration_seconds':duration/1000})
    return sorted(out,key=lambda s:s['start'])
