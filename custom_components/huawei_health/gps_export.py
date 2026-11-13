"""Explicit, local GPX export from recorded MotionPath data. No external uploads."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from datetime import date, datetime, timezone
import xml.etree.ElementTree as ET

PATH = '/dataQuery/path/getMotionPathData'
NS = 'http://www.topografix.com/GPX/1/1'
EXT = 'urn:huawei-health-local-export:1'


def numeric(value):
    if isinstance(value, bool):return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):return None


def records(payload, depth=0):
    if depth > 10:return
    if isinstance(payload, dict):
        if all(k in payload for k in ('startTime','totalDistance','totalTime')):
            yield payload
        else:
            for value in payload.values():
                if isinstance(value,(dict,list)):yield from records(value,depth+1)
    elif isinstance(payload,list):
        for value in payload:
            if isinstance(value,(dict,list)):yield from records(value,depth+1)


def sequence(row):
    raw = row.get('attribute')
    if not isinstance(raw,str):return ''
    first = raw.find('@is'); sep=raw.find('&&')
    if first < 0 or sep < first+3:return ''
    return raw[first+3:sep]



def altitude_samples(row):
    """alti.k is epoch milliseconds; alti.v is metres in the tested record."""
    samples={}
    for line in sequence(row).splitlines():
        values={}
        for token in line.split(';'):
            if '=' in token:
                k,v=token.split('=',1);values[k.strip()]=v.strip()
        if values.get('tp')!='alti':continue
        t=numeric(values.get('k'));v=numeric(values.get('v'))
        if t is None or v is None or not 946684800000<=t<=4102444800000:continue
        samples[t/1000]=v
    return sorted(samples.items())


def points(row):
    out=[]
    for line in sequence(row).splitlines():
        fields={}
        for token in line.split(';'):
            if '=' in token:
                k,v=token.split('=',1);fields[k.strip()]=v.strip()
        if fields.get('tp')!='lbs':continue
        lat,lon,t=(numeric(fields.get(k)) for k in ('lat','lon','t'))
        if lat is None or lon is None or t is None:continue
        if not (-90<lat<90 and -180<=lon<=180) or (lat==0 and lon==0):continue
        # v6 confirms epoch seconds in t; fail closed for other timestamp formats.
        if not 946684800<=t<=4102444800:continue
        out.append({'lat':lat,'lon':lon,'time':t,'ele':numeric(fields.get('alt'))})
    if len(out)<2:raise ValueError('no_valid_route')
    if any(b['time']<a['time'] for a,b in zip(out,out[1:])):
        raise ValueError('nonmonotonic_route_time')
    start,end=numeric(row.get('startTime')),numeric(row.get('endTime'))
    if start is not None and end is not None:
        if out[0]['time']<start/1000-60 or out[-1]['time']>end/1000+60:
            raise ValueError('route_outside_workout')
    # This account returns alt=0 for every GPS point, while the original export
    # has 128-130 m. Do not present an all-zero source as measured elevation.
    if all(p['ele'] is None or p['ele'] == 0 for p in out):
        for p in out:p['ele'] = None
    # Hold last preceding measured altitude, at most 30 s old. No linear
    # interpolation, backward fill, or height invented from summary extrema.
    from bisect import bisect_right
    measured=altitude_samples(row)
    times=[t for t,_ in measured]
    for p in out:
        p['elevation_source']='gps' if p['ele'] is not None else 'missing'
        index=bisect_right(times,p['time'])-1
        if index>=0 and p['time']-times[index]<=30:
            p['ele']=measured[index][1]
            p['elevation_source']='alti_previous_sample'
    return out


def iso(seconds):
    return datetime.fromtimestamp(seconds,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


def build_gpx(row):
    route=points(row)
    ET.register_namespace('',NS);ET.register_namespace('hh',EXT)
    root=ET.Element('{'+NS+'}gpx',{'version':'1.1','creator':'Huawei Health local MotionPath export'})
    trk=ET.SubElement(root,'{'+NS+'}trk')
    kind='Walking' if numeric(row.get('sportType'))==5 else 'Workout'
    ET.SubElement(trk,'{'+NS+'}name').text=kind+' '+iso(route[0]['time'])[:10]
    ET.SubElement(trk,'{'+NS+'}type').text=kind.lower()
    ext=ET.SubElement(trk,'{'+NS+'}extensions')
    for source,target in [('totalDistance','distance_m'),('totalTime','active_duration_ms')]:
        v=numeric(row.get(source))
        if v is not None:ET.SubElement(ext,'{'+EXT+'}'+target).text=str(v)
    seg=None;last=None
    for point in route:
        if seg is None or (last is not None and point['time']-last>30):
            seg=ET.SubElement(trk,'{'+NS+'}trkseg')
        p=ET.SubElement(seg,'{'+NS+'}trkpt',{'lat':format(point['lat'],'.10f'),'lon':format(point['lon'],'.10f')})
        if point['ele'] is not None:ET.SubElement(p,'{'+NS+'}ele').text=str(point['ele'])
        ET.SubElement(p,'{'+NS+'}time').text=iso(point['time'])
        last=point['time']
    return ET.tostring(root,encoding='utf-8',xml_declaration=True),len(route)



def build_tcx(row):
    """All-workout TCX with optional GPS and verified HR."""
    from .workout_export import build_tcx as build
    payload, info = build(row)
    return payload, info['gps_points']



def export_day(client, day_text, directory, file_format="gpx"):
    if file_format not in ("gpx", "tcx"):raise ValueError("invalid_format")
    chosen=date.fromisoformat(day_text);day=int(chosen.strftime('%Y%m%d'))
    answer=client.post(PATH,{'startTime':day,'endTime':day,'queryType':1,'dataType':2})
    directory=Path(directory);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    result={'day':day_text,'exports':[],'skipped_without_valid_route':0,
            'note':'GPX files contain precise locations. Keep private. No external upload performed.'}
    seen=set()
    for row in records(answer):
        key=str(row.get('recordId') or (row.get('startTime'),row.get('endTime'),row.get('deviceCode')))
        digest=hashlib.sha256(key.encode()).hexdigest()[:16]
        if digest in seen:continue
        seen.add(digest)
        if len(seen)>100:raise ValueError('too_many_records')
        try:payload,count=(build_tcx(row) if file_format == "tcx" else build_gpx(row))
        except ValueError:
            result['skipped_without_valid_route']+=1;continue
        target=directory/(day_text+'_'+digest+'.'+file_format)
        # Private temporary file, atomically replace only this export's stable target.
        import tempfile
        fd,tmp=tempfile.mkstemp(prefix='.gpx-',dir=directory)
        try:
            with os.fdopen(fd,'wb') as f:f.write(payload)
            os.replace(tmp,target)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        from .workout_export import route
        mapped=route(row)
        result['exports'].append({'file':str(target),'gps_points':count,
                                  'altitude_points':sum(p['ele'] is not None for p in mapped),
                                  'altitude_from_alti':sum(p.get('elevation_source')=='alti_previous_sample' for p in mapped),
                                  'distance_m':numeric(row.get('totalDistance')),
                                  'duration_seconds':(numeric(row.get('totalTime')) or 0)/1000})
    return result


def register(hass):
    """Register a manual action; never executes automatically during polling."""
    import asyncio
    import voluptuous as vol
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError
    domain='huawei_health'
    lock=asyncio.Lock()
    async def handle(call):
        entry_id=call.data.get('entry_id')
        entries=[e for e in hass.config_entries.async_entries(domain)
                 if getattr(getattr(e,'runtime_data',None),'client',None) is not None
                 and (not entry_id or e.entry_id==entry_id)]
        if len(entries)!=1:
            raise HomeAssistantError('Select exactly one loaded Huawei Health entry using entry_id.')
        try:date.fromisoformat(call.data['day'])
        except (ValueError,TypeError):raise HomeAssistantError('day must be YYYY-MM-DD') from None
        async with lock:
            try:
                return await hass.async_add_executor_job(export_day,entries[0].runtime_data.client,
                    call.data['day'],hass.config.path('huawei_health_exports'),
                    'tcx' if call.service == 'export_tcx' else 'gpx')
            except Exception as err:
                # Client errors can contain response bodies. Never forward them to UI/logs.
                raise HomeAssistantError('Workout export failed ('+type(err).__name__+'). No upload performed.') from None
    for name in ('export_gpx', 'export_tcx'):
        if not hass.services.has_service(domain,name):
            hass.services.async_register(domain,name,handle,
                schema=vol.Schema({vol.Required('day'):str,vol.Optional('entry_id'):str}),
                supports_response=SupportsResponse.OPTIONAL)
