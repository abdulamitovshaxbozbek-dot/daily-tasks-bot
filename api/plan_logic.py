"""Deterministic planning math for the Qadam plan builder.

All numbers (daily quota, buffer days, feasibility) come from this module.
The AI only names stages and chats; it never decides quantities.
"""
import math
import re
from datetime import timedelta

MAX_DAYS = 90
MAX_TOTAL = 100000
MILESTONES = (25, 50, 75, 100)
QUANTITY_UNITS = {"km", "soat", "daqiqa"}
DEFAULT_MPU = {"bet": 2, "bob": 40, "mavzu": 45, "dars": 45, "video": 30,
               "masala": 10, "test": 2, "savol": 2, "so‘z": 1, "km": 6, "mashq": 10}
BUFFER_TITLE = "Zaxira kun: orqada qolgan bo‘lsangiz yetib oling, bo‘lmasa dam oling"

_APOS = "['‘’ʼʻ`]"
_UNIT_PATTERNS = [
    (r"bet(?:li|lik)?|sahifa(?:li|lik)?|pages?|varaq", "bet"),
    (r"bob(?:li|lik)?|chapters?", "bob"),
    (r"mavzu(?:li|lik)?", "mavzu"),
    (r"dars(?:li|lik)?|lessons?", "dars"),
    (r"video(?:li|lik)?", "video"),
    (r"so" + _APOS + r"?z(?:li|lik)?|words?", "so‘z"),
    (r"km|kilometr(?:li|lik)?", "km"),
    (r"masala(?:li|lik)?", "masala"),
    (r"test(?:li|lik)?", "test"),
    (r"savol(?:li|lik)?", "savol"),
    (r"mashq(?:li|lik)?", "mashq"),
    (r"soat(?:li|lik)?", "soat"),
]
_NUMBER = r"(\d{1,3}(?:[ .,]\d{3})+|\d+)"
_STAGE_WORDS = re.compile(
    r"tayyorlan|imtihon|loyiha|sayt|dastur|o" + _APOS + r"?rgan|ko" + _APOS +
    r"?ch|tashkil|yarat|ielts|topshir|sertifikat|qur(?:ish|moq)|bitir|portfolio",
    re.IGNORECASE)
_RAMP_WORDS = re.compile(
    r"odat|boshla|yugur|sport|mashq qil|har kuni|kundalik|mashg" + _APOS + r"?ul|meditatsiya|sayr",
    re.IGNORECASE)


def clean_title(text):
    text = re.sub(r"\s+", " ", (text or "").strip()).strip(" .!?")
    return (text[:1].upper() + text[1:])[:100]


def parse_goal(text):
    """Heuristic goal reading. kind is None when it cannot be guessed."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    result = {"title": clean_title(text), "total": None, "unit": None, "kind": None}
    for pattern, unit in _UNIT_PATTERNS:
        match = re.search(_NUMBER + r"\s*(?:ta\s+)?(?:" + pattern + r")(?![A-Za-z])", text, re.IGNORECASE)
        if match:
            total = int(re.sub(r"[ .,]", "", match.group(1)))
            if 2 <= total <= MAX_TOTAL:
                result["total"], result["unit"], result["kind"] = total, unit, "volume"
                return result
    if _STAGE_WORDS.search(text):
        result["kind"] = "stages"
    elif _RAMP_WORDS.search(text):
        result["kind"] = "ramp"
    return result


def schedule_days(start, span, weekdays):
    weekdays = set(weekdays)
    return [start + timedelta(days=i) for i in range(span)
            if (start + timedelta(days=i)).weekday() in weekdays]


def _buffer_flags(count, use_buffer):
    flags = [False] * count
    if use_buffer and count >= 7:
        for i in range(6, count - 1, 7):
            flags[i] = True
    return flags


def study_day_count(start, span, weekdays, buffer=True):
    days = schedule_days(start, span, weekdays)
    return len([f for f in _buffer_flags(len(days), buffer) if not f])


def unit_text(unit, a, b=None):
    unit = unit or "ta"
    if unit in QUANTITY_UNITS:
        return "%s %s" % (b - a + 1 if b is not None else a, unit)
    if b is None or a == b:
        return "%s-%s" % (a, unit)
    return "%s–%s-%s" % (a, b, unit)


def build_volume(title, total, unit, days_list, mpu=None, buffer=True, offset=0):
    """Split total units over days_list. offset = units already done."""
    if not days_list:
        raise ValueError("no days")
    flags = _buffer_flags(len(days_list), buffer)
    work = [i for i, f in enumerate(flags) if not f]
    count = min(len(work), total)
    if count <= 0:
        raise ValueError("nothing to schedule")
    base, extra = divmod(total, count)
    steps, position, last_work = [], offset + 1, work[count - 1]
    sizes = {work[j]: base + (1 if j < extra else 0) for j in range(count)}
    for i, day in enumerate(days_list):
        if i > last_work:
            break
        if flags[i]:
            steps.append({"day": day, "title": BUFFER_TITLE, "units": None,
                          "minutes": None, "is_buffer": True})
            continue
        size = sizes[i]
        end = position + size - 1
        steps.append({"day": day, "title": "%s: %s" % (title, unit_text(unit, position, end)),
                      "units": size, "minutes": round(size * mpu) if mpu else None,
                      "is_buffer": False, "unit_start": position, "unit_end": end})
        position = end + 1
    return steps


def remaining_ranges(steps, total, unit):
    """Return uncompleted quantity intervals, preserving out-of-order work.

    Older numbered plans can recover their intervals from the generated suffix.
    Older quantity plans cannot recover order after a previous redistribution;
    reject those ambiguous records instead of silently repeating work.
    """
    completed = []
    ordered = sorted(steps, key=lambda s: (s.get("position", 0), s.get("id", 0)))
    cursor = 1
    for step in ordered:
        if step.get("is_buffer") or not step.get("units"):
            continue
        first, last = step.get("unit_start"), step.get("unit_end")
        if first is None or last is None:
            suffix = re.search(r"(\d+)(?:[–-](\d+))?-" + re.escape(unit or "ta") + r"$",
                               step.get("title", "")) if unit not in QUANTITY_UNITS else None
            if suffix:
                first = int(suffix.group(1))
                last = int(suffix.group(2) or suffix.group(1))
            elif any(s.get("carried") for s in ordered):
                raise ValueError("legacy quantity ranges are ambiguous")
            else:
                first, last = cursor, cursor + step["units"] - 1
        cursor = last + 1
        if step["status"] == "done":
            completed.append((first, last))
    result, cursor = [], 1
    for first, last in sorted(completed):
        first, last = max(1, first), min(total, last)
        if first > cursor:
            result.append((cursor, first - 1))
        cursor = max(cursor, last + 1)
    if cursor <= total:
        result.append((cursor, total))
    return result


def build_remaining_volume(title, ranges, unit, days_list, mpu=None):
    """Split remaining intervals without assigning completed units again."""
    count = sum(b - a + 1 for a, b in ranges)
    quotas = build_volume(title, count, unit, days_list, mpu=mpu)
    intervals = list(ranges)
    result = []
    for quota in quotas:
        if quota["is_buffer"]:
            result.append(quota)
            continue
        left = quota["units"]
        while left:
            first, last = intervals[0]
            size = min(left, last - first + 1)
            end = first + size - 1
            result.append(dict(quota, title="%s: %s" % (title, unit_text(unit, first, end)),
                               units=size, minutes=round(size * mpu) if mpu else None,
                               unit_start=first, unit_end=end))
            left -= size
            if end == last:
                intervals.pop(0)
            else:
                intervals[0] = (end + 1, last)
    return result


def _round5(value):
    return max(5, int(round(value / 5.0)) * 5)


def build_ramp(title, days_list, start_minutes, target_minutes):
    if not days_list:
        raise ValueError("no days")
    target_minutes = max(target_minutes, start_minutes)
    first = days_list[0]
    weeks = (days_list[-1] - first).days // 7 + 1
    steps = []
    for day in days_list:
        week = (day - first).days // 7
        minutes = start_minutes if weeks == 1 else start_minutes + (target_minutes - start_minutes) * week / (weeks - 1)
        minutes = _round5(minutes)
        steps.append({"day": day, "title": "%s: %s daqiqa" % (title, minutes), "units": None,
                      "minutes": minutes, "is_buffer": False})
    return steps


def spread_stages(stages, days_list, dense=False):
    """Place ordered stages on days. Extra stages are merged into the last slot."""
    n, m = len(days_list), len(stages)
    if n == 0 or m == 0:
        raise ValueError("nothing to place")
    stages = [dict(s) for s in stages]
    if m > n:
        tail = stages[n - 1:]
        stages = stages[:n - 1] + [{"title": "; ".join(s["title"] for s in tail)[:120],
                                    "minutes": sum(s.get("minutes") or 0 for s in tail) or None}]
        m = n
    positions, previous = [], -1
    for i in range(m):
        raw = i if dense else (i * (n - 1) / (m - 1) if m > 1 else 0)
        index = max(int(raw + 0.5), previous + 1)
        index = min(index, n - (m - i))
        positions.append(index)
        previous = index
    return [{"day": days_list[idx], "title": s["title"][:120], "units": None,
             "minutes": s.get("minutes"), "is_buffer": False}
            for idx, s in zip(positions, stages)]


def extend_span(start, weekdays, needed_study_days, minimum_span, buffer=True):
    """Smallest calendar span (>= minimum_span) with enough study days."""
    for span in range(max(minimum_span, 1), 366):
        if study_day_count(start, span, weekdays, buffer) >= needed_study_days:
            return span
    return 365


def feasibility(kind, available_minutes, days_list, start, weekdays, total=None, mpu=None,
                stages=None, start_minutes=None, target_minutes=None, buffer=True):
    """Honest check: can the person do this in the time they have?"""
    result = {"ok": True, "needed_minutes": None, "available_minutes": available_minutes,
              "suggest_days": None, "suggest_minutes": None}
    if not available_minutes or not days_list:
        return result
    flags = _buffer_flags(len(days_list), buffer)
    study = len([f for f in flags if not f])
    if kind == "volume" and total and mpu:
        per_day = math.ceil(total / max(1, min(study, total)))
        needed = per_day * mpu
        result["needed_minutes"] = int(math.ceil(needed))
        if needed > available_minutes * 1.05:
            result["ok"] = False
            result["suggest_minutes"] = int(math.ceil(needed / 5.0) * 5)
            capacity_units = max(1, int(available_minutes // mpu))
            result["suggest_days"] = extend_span(start, weekdays, math.ceil(total / capacity_units),
                                                 len(days_list), buffer)
    elif kind == "stages" and stages:
        total_minutes = sum(s.get("minutes") or 0 for s in stages)
        per_day = total_minutes / max(1, study)
        result["needed_minutes"] = int(math.ceil(per_day))
        if per_day > available_minutes * 1.05:
            result["ok"] = False
            result["suggest_minutes"] = int(math.ceil(per_day / 5.0) * 5)
            needed_study = math.ceil(total_minutes / available_minutes)
            result["suggest_days"] = extend_span(start, weekdays, needed_study, len(days_list), buffer)
    elif kind == "ramp" and target_minutes:
        result["needed_minutes"] = int(target_minutes)
        if target_minutes > available_minutes * 1.05:
            result["ok"] = False
            result["suggest_minutes"] = int(target_minutes)
    return result


def progress_percent(kind, total, steps):
    real = [s for s in steps if not s.get("is_buffer") and not s.get("carried")]
    if not real:
        return 0
    if kind == "volume" and total:
        done = sum((s.get("units") or 0) for s in real if s["status"] == "done")
        return min(100, int(done * 100 / total))
    return int(sum(s["status"] == "done" for s in real) * 100 / len(real))


def today_step(steps, today):
    candidates = [s for s in steps if s["day"] == today and not s.get("is_buffer")
                  and not s.get("carried")]
    return next((s for s in candidates if s["status"] == "pending"),
                candidates[0] if candidates else None)


def next_milestone(percent, last):
    reached = [m for m in MILESTONES if m <= percent and m > last]
    return max(reached) if reached else 0


def streak(steps, today):
    real = sorted((s for s in steps if not s.get("is_buffer") and s["day"] <= today),
                  key=lambda s: s["day"], reverse=True)
    by_day = {}
    for step in real:
        by_day.setdefault(step["day"], []).append(step)
    count = 0
    for day in sorted(by_day, reverse=True):
        statuses = [s["status"] for s in by_day[day]]
        if all(status == "done" for status in statuses):
            count += 1
        elif day == today and all(status in ("done", "pending") for status in statuses):
            continue
        else:
            break
    return count


def unanswered_past(steps, today):
    return [s for s in steps if not s.get("is_buffer") and s["day"] < today and s["status"] == "pending"]


def progress_bar(percent, width=10):
    filled = int(round(width * percent / 100.0))
    return "▓" * filled + "░" * (width - filled)
