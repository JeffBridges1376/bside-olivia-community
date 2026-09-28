"""Stable character timetable and timestamped Shanghai observations."""
from datetime import datetime, timedelta, timezone
import math
import httpx

LOCAL = timezone(timedelta(hours=8))
# Character-world timetable, not the university's published timetable.
WEEK = (
    ((9, 0, 100, '和声'), (14, 0, 60, '钢琴专业课')),
    ((9, 0, 100, '大学外语'), (14, 0, 100, '心理学'), (16, 0, 50, '体育')),
    ((10, 0, 100, '音乐史'),),
    ((9, 0, 100, '视唱练耳'), (14, 0, 100, '室内乐排练')),
    ((9, 0, 100, '公共课'), (14, 0, 60, '演奏交流')),
    (), (),
)


def student_schedule(now):
    local = now.astimezone(LOCAL)
    day = local.date()
    result = {'date': day.isoformat(), 'basis': '角色学期安排，并非学校官方课表',
              'phase': 'teaching', 'year': local.year - 2025 + (local.month >= 9),
              'classes': [], 'current_class': None, 'next_class': None,
              'meaning': '课表是计划，不证明已经出席、完成作业或身在学校；往返需要时间。'}
    if day.isoformat() < '2025-09-01':
        result['phase'] = 'before_enrollment'
    elif day.isoformat() >= '2029-07-01':
        result['phase'] = 'graduated'
    elif local.month in (2, 7, 8) or (local.month == 1 and local.day >= 20):
        result['phase'] = 'vacation'
    elif (local.month == 10 and local.day <= 7) or (local.month == 5 and local.day <= 3) or (local.month == 1 and local.day == 1):
        result['phase'] = 'holiday'
    elif local.month in (1, 6) and local.day >= 8:
        result['phase'] = 'assessment'
    if result['phase'] == 'teaching':
        for hour, minute, duration, title in WEEK[local.weekday()]:
            start = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            end = start + timedelta(minutes=duration)
            entry = {'title': title, 'start': start.isoformat(), 'end': end.isoformat()}
            result['classes'].append(entry)
            if start <= local < end:
                result['current_class'] = entry
            elif start > local and result['next_class'] is None:
                result['next_class'] = entry
    return result


def weather_view(value, now):
    if not value:
        return {'status': 'unknown', 'city': '上海'}
    try:
        age = (now - datetime.fromisoformat(value['observed_at'])).total_seconds()
        temperature = float(value['temperature_c'])
        if not math.isfinite(temperature) or not -50 <= temperature <= 60:
            raise ValueError('invalid temperature')
    except (ValueError, TypeError, KeyError):
        return {'status': 'unknown', 'city': '上海'}
    return {**value, 'status': 'fresh' if 0 <= age <= 7200 else 'stale',
            'meaning': '上海虹桥站实测，仅代表观测时站点天气；不能断言住处窗外正在下雨。过期不可当作当前天气。'}


async def shanghai_weather(now):
    """Public station observation; no model inference or private data sent."""
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get('https://aviationweather.gov/api/data/metar',
                params={'ids': 'ZSSS', 'format': 'json'}, headers={'User-Agent': 'Olivia-World/1.0'})
            response.raise_for_status()
            rows = response.json()
        if not isinstance(rows, list):
            return None
        row = max((r for r in rows if isinstance(r, dict) and r.get('icaoId') == 'ZSSS'), key=lambda r: r['obsTime'])
        observed = datetime.fromtimestamp(row['obsTime'], timezone.utc)
        temp = float(row['temp'])
        if not math.isfinite(temp) or not -50 <= temp <= 60 or not 0 <= (now - observed).total_seconds() <= 7200:
            return None
        return {'city': '上海', 'station': 'ZSSS', 'source': 'NOAA Aviation Weather Center',
                'observed_at': observed.isoformat(), 'temperature_c': temp,
                'cloud_cover': str(row.get('cover') or '')[:16],
                'weather_codes': str(row.get('wxString') or '')[:64]}
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError):
        return None
