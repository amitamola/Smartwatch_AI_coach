"""Coverage-aware scale trends; no body-composition conclusions from weight alone."""

import math
import coach_clock
from collections import defaultdict
from datetime import date, timedelta


def weight_trend(rows, today=None):
    today = today or coach_clock.today()
    daily = defaultdict(list)
    for row in rows:
        value = row.get("weight")
        try:
            day = date.fromisoformat(str(row.get("calendarDate", ""))[:10])
        except ValueError:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0 or day > today):
            continue
        if day >= today - timedelta(days=29):
            daily[day].append(value / 1000)
    if not daily:
        return None
    series = [{"date": d.isoformat(), "weight_kg": round(sum(v) / len(v), 2),
               "measurements": len(v)} for d, v in sorted(daily.items())]
    means = {date.fromisoformat(r["date"]): r["weight_kg"] for r in series}
    recent = [v for d, v in means.items() if d >= today - timedelta(days=6)]
    previous = [v for d, v in means.items() if today - timedelta(days=13) <= d < today - timedelta(days=6)]
    latest = series[-1]
    return {"series": series, "count": len(series), "latest_kg": latest["weight_kg"],
            "latest_date": latest["date"],
            "age_days": (today - date.fromisoformat(latest["date"])).days,
            "average_7d_kg": round(sum(recent) / len(recent), 2) if recent else None,
            "previous_7d_average_kg": round(sum(previous) / len(previous), 2) if previous else None,
            "measured_days_7d": len(recent), "measured_days_previous_7d": len(previous),
            "change_last_7d_kg": (round(sum(recent) / len(recent) - sum(previous) / len(previous), 2)
                                   if recent and previous else None),
            "interpretation": "Daily means, equally weighted across observed days; missing days are unknown. "
                              "Sparse coverage limits comparison. Weight change is not measured fat or muscle change."}
