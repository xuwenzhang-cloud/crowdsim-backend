"""Opt-in activity clock for confirmed class-one memorial-tower requirements."""

import math


ACTIVITY_POLICY = "requirement-hotspot-times-v1"


def requirement_hotspot_activity(requirement):
    if (requirement.get("schema_version") != 2
            or requirement.get("research", {}).get("problem_type") != 1
            or requirement.get("spatial_scope", {}).get("location_id") != "memorial-tower"
            or "hotspot_start_seconds" not in requirement.get("scenario", {})):
        return None  # Other entries and saved requirements keep their original preset.
    scenario = requirement["scenario"]
    start, end = scenario["hotspot_start_seconds"], scenario.get("hotspot_end_seconds")
    values = (start,) if end is None else (start, end)
    for value in values:
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("热点活动时间必须为有限非负秒数")
    if end is not None and end <= start:
        raise ValueError("热点活动结束时间必须晚于开始时间")
    return {"policy": ACTIVITY_POLICY, "start_seconds": start, "end_seconds": end}


def activity_phase(activity, now):
    if now < activity["start_seconds"]:
        return "before_start"
    if activity["end_seconds"] is not None and now >= activity["end_seconds"]:
        return "ended"
    return "active"
