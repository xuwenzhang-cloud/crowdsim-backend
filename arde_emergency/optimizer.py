"""Emergency ARDE optimizer for CrowdSim decision optimization.

Follows the same request/response habit as decision_evaluation_engine.py:
the frontend (or a CLI) submits evaluation + workflow JSON; this module
returns a plan that DecisionOptimization.savePlan() can persist, plus an
aggregatedPolicy that OverlayNetworkSimulator.apply_policy() can execute.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from .policy_registry import ARDE_PRESETS, apply_payload_overrides


ENGINE_ID = "ARDE-Emergency-v1"
METHOD_ID = "bilevel-autonomy"
METHOD_NAME = "双层自治理优化"
TOTAL_OFFICERS = 24


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _text(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def infer_emergency_profile(evaluation: Dict[str, Any], workflow: Dict[str, Any], options: Dict[str, Any]) -> str:
    explicit = str(options.get("emergencyProfile") or "").strip()
    if explicit in {"crowd_crush", "urban_flood_720", "combined"}:
        return explicit

    blob = _text(
        evaluation.get("scenario"),
        evaluation.get("location"),
        evaluation.get("primaryWeakness"),
        workflow.get("scenario", {}).get("eventType") if isinstance(workflow.get("scenario"), dict) else "",
        workflow.get("scenario", {}).get("text") if isinstance(workflow.get("scenario"), dict) else "",
        (workflow.get("scenario") or {}).get("triggeredEvent", {}).get("eventType")
        if isinstance(workflow.get("scenario"), dict) else "",
        (workflow.get("scenario") or {}).get("triggeredEvent", {}).get("scenarioTitle")
        if isinstance(workflow.get("scenario"), dict) else "",
    ).lower()

    flood = any(token in blob for token in ("flood", "water", "rain", "积水", "内涝", "暴雨", "排水", "720"))
    crowd = any(token in blob for token in ("crowd", "聚集", "踩踏", "密度", "分流", "限流", "对冲"))
    if flood and crowd:
        return "combined"
    if flood:
        return "urban_flood_720"
    return "crowd_crush"


def infer_weakness_angle(primary_weakness: str) -> str:
    text = str(primary_weakness or "")
    if any(token in text for token in ("转移", "外围", "二次", "spillover")):
        return "spillover"
    if any(token in text for token in ("延迟", "服从", "岗位", "广播")):
        return "execution"
    if any(token in text for token in ("阈值", "时机", "误触发", "升级")):
        return "timing"
    if any(token in text for token in ("成本", "资源", "权衡")):
        return "resource"
    if any(token in text for token in ("封控", "排水", "通道")):
        return "flood_sync"
    return "composite"


def _metrics_from_payload(evaluation: Dict[str, Any], workflow: Dict[str, Any], experiment: Dict[str, Any]) -> Dict[str, float]:
    source: Dict[str, Any] = {}
    experiment_avg = {}
    if isinstance(workflow.get("experiment"), dict):
        experiment_avg = workflow["experiment"].get("averageMetrics") or {}
    if isinstance(experiment_avg, dict) and experiment_avg:
        source = experiment_avg
    elif isinstance(experiment.get("baselineMetrics"), dict):
        source = experiment["baselineMetrics"]
    elif isinstance(evaluation.get("metricComparisons"), list):
        source = {
            item.get("key"): item.get("after", item.get("before"))
            for item in evaluation["metricComparisons"]
            if isinstance(item, dict) and item.get("key")
        }

    defaults = {
        "density": 4.3,
        "speed": 0.72,
        "pressure": 72.0,
        "riskIndex": 72.0,
        "congestionMinutes": 18.0,
    }
    return {key: _number(source.get(key), default) for key, default in defaults.items()}


def _flood_depth(evaluation: Dict[str, Any], workflow: Dict[str, Any], experiment: Dict[str, Any]) -> float:
    triggered = {}
    if isinstance(workflow.get("scenario"), dict):
        triggered = workflow["scenario"].get("triggeredEvent") or {}
    return _clamp(
        _number(
            triggered.get("floodDepth"),
            _number(evaluation.get("floodDepth"), _number(experiment.get("floodDepth"), 0.0)),
        ),
        0.0,
        1.5,
    )


def infer_phase(metrics: Dict[str, float], flood_depth: float, final_score: float) -> str:
    if metrics["riskIndex"] < 38 and metrics["density"] < 2.6 and flood_depth < 0.12 and final_score >= 86:
        return "recovery"
    if metrics["density"] >= 3.4 or metrics["riskIndex"] >= 62 or flood_depth >= 0.18 or final_score < 82:
        return "response"
    return "warning"


def _region_catalog(profile: str) -> List[Dict[str, str]]:
    if profile == "urban_flood_720":
        # 郑州「7·20」主控分区：与事前洪水热点 jingguang / metro5 / central 对齐
        return [
            {"id": "tunnel", "label": "京广快速路北隧道"},
            {"id": "metro", "label": "地铁5号线五龙口段"},
            {"id": "bypass", "label": "中心城区绕行通道"},
        ]
    return [
        {"id": "hotspot", "label": "观景平台/热点区"},
        {"id": "metro", "label": "地铁出入口"},
        {"id": "corridor", "label": "步行廊道/外围通道"},
    ]


def _upper_layer(
    profile: str,
    phase: str,
    angle: str,
    metrics: Dict[str, float],
    flood_depth: float,
) -> Dict[str, Any]:
    regions = _region_catalog(profile)
    if profile == "urban_flood_720":
        order = ["tunnel", "metro", "bypass"]
    elif angle == "spillover":
        order = ["hotspot", "corridor", "metro"]
    else:
        order = ["hotspot", "metro", "corridor"]

    if phase == "response":
        shares = [12, 8, 4]
    elif phase == "recovery":
        shares = [8, 8, 8]
    else:
        shares = [10, 8, 6]

    allocation = {}
    remaining = TOTAL_OFFICERS
    for index, region_id in enumerate(order):
        value = shares[index] if index < len(shares) - 1 else remaining
        allocation[region_id] = value
        remaining -= value

    rules = [
        {
            "condition": "5 分钟内主风险区密度降幅 < 10%",
            "nextPhase": "response",
            "action": "升级硬隔离 / 红色处置",
        },
        {
            "condition": "外围或廊道密度连续 3 分钟上升",
            "nextPhase": "response",
            "action": "立即加开第二分流路径并同步限流",
        },
    ]
    if profile in {"urban_flood_720", "combined"}:
        rules.append({
            "condition": f"积水深度持续高于 {max(0.18, flood_depth):.2f} m 且仍有人员进入",
            "nextPhase": "response",
            "action": "深水区硬封控 + 加大排水衰减",
        })
    if metrics["riskIndex"] < 40:
        rules.append({
            "condition": "风险指数连续两个窗口低于 40",
            "nextPhase": "recovery",
            "action": "平滑降级，防止回流二次聚集",
        })

    return {
        "emergencyPhase": phase,
        "regionPriorities": order,
        "regionLabels": {item["id"]: item["label"] for item in regions},
        "resourceAllocation": allocation,
        "phaseSwitchRules": rules,
        "focus": angle,
        "profile": profile,
    }


def _lower_actions(profile: str, phase: str, angle: str, upper: Dict[str, Any]) -> List[Dict[str, Any]]:
    labels = upper["regionLabels"]
    actions: List[Dict[str, Any]] = []

    if profile == "urban_flood_720":
        catalog = {
            "tunnel": {
                "decision": "arde_flood_response",
                "actions": ["深水区硬封控", "移动排水", "禁止进入口部"],
                "problem": "京广北隧道积水加深，封控过晚或排水与疏散不同步",
                "technique": "先封、再排、再导",
            },
            "metro": {
                "decision": "arde_flood_response",
                "actions": ["口部限流", "积水边界广播", "应急通道保护"],
                "problem": "地铁5号线口部仍有人员进入积水影响区",
                "technique": "限流 + 排水窗口内的高地引导",
            },
            "bypass": {
                "decision": "temporary_diversion",
                "actions": ["高地绕行", "动态警戒带", "绕行通道承载监测"],
                "problem": "中心城区绕行导致金水/中原外围过载",
                "technique": "第二路径分流并校验容量",
            },
        }
    else:
        catalog = {
            "hotspot": {
                "decision": "arde_crowd_response",
                "actions": ["入口截流 20%", "单向放行", "分区短句广播"],
                "problem": "热点密度超阈且对冲人流持续",
                "technique": "截流 + 单向化 + 广播",
            },
            "metro": {
                "decision": "arde_crowd_response" if profile != "combined" else "arde_combined_response",
                "actions": ["口部限流", "硬封控深水入口" if profile == "combined" else "转向点加岗", "积水消退/禁止再进入"],
                "problem": "出入口成为转移堵点或积水暴露点",
                "technique": "封控/限流与排水或岗位协同",
            },
            "corridor": {
                "decision": "temporary_diversion",
                "actions": ["第二分流路径", "限流 15%", "转向点引导员"],
                "problem": "核心区减压后风险向外围转移",
                "technique": "与热点同步限流，抑制 spillover",
            },
        }
        if angle == "execution":
            catalog["hotspot"]["actions"].append("固定岗位责任区")
            catalog["corridor"]["actions"].append("统一引导口径")

    if phase == "warning":
        for item in catalog.values():
            item["decision"] = "arde_warning"
            item["actions"] = ["岗位预置", "广播提示", "轻度限流"]
    elif phase == "recovery":
        for item in catalog.values():
            item["decision"] = "arde_recovery"

    for region_id in upper["regionPriorities"]:
        item = catalog.get(region_id)
        if not item:
            continue
        actions.append({
            "regionId": region_id,
            "region": labels.get(region_id, region_id),
            "decision": item["decision"],
            "actions": item["actions"],
            "problem": item["problem"],
            "technique": item["technique"],
            "params": {
                "containmentLevel": 78 if phase == "response" else 52 if phase == "warning" else 48,
            },
        })
    return actions


def _aggregate_policy(profile: str, phase: str, angle: str, flood_depth: float) -> Dict[str, Any]:
    if phase == "warning":
        name = "arde_warning"
    elif phase == "recovery":
        name = "arde_recovery"
    elif profile == "urban_flood_720":
        name = "arde_flood_response"
    elif profile == "combined":
        name = "arde_combined_response"
    else:
        name = "arde_crowd_response"

    payload = dict(ARDE_PRESETS[name])
    if angle == "spillover":
        payload["diversion"] = _clamp(payload["diversion"] + 0.08, 0.0, 1.0)
        payload["capacity_multiplier"] = _clamp(payload["capacity_multiplier"] + 0.08, 0.5, 3.5)
    if profile in {"urban_flood_720", "combined"} and flood_depth >= 0.18:
        payload["water_decay_per_second"] = _clamp(payload["water_decay_per_second"] + 0.006, 0.0, 0.08)
    payload["containmentLevel"] = round(payload["strength"] * 100.0, 1)
    for key in ("strength", "capacity_multiplier", "speed_recovery", "water_decay_per_second", "diversion", "visual_relief"):
        payload[key] = round(float(payload[key]), 3)
    apply_payload_overrides(payload, payload)
    return {"decision": name, "payload": payload}


def _expected_metrics(metrics: Dict[str, float], phase: str, angle: str, profile: str) -> Dict[str, float]:
    density_cut = 0.22 if phase == "response" else 0.10 if phase == "warning" else 0.16
    if angle == "spillover":
        density_cut += 0.04
    if profile == "combined":
        density_cut += 0.03
    speed_gain = 0.28 if phase == "response" else 0.12
    return {
        "density": round(metrics["density"] * (1.0 - density_cut), 3),
        "speed": round(metrics["speed"] + (1.45 - metrics["speed"]) * speed_gain, 3),
        "pressure": round(metrics["pressure"] * (1.0 - density_cut * 1.15), 2),
        "riskIndex": round(metrics["riskIndex"] * (1.0 - density_cut * 1.35), 2),
        "congestionMinutes": round(metrics["congestionMinutes"] * (1.0 - density_cut * 1.1), 2),
    }


def _ui_metric_rows(current: Dict[str, float], target: Dict[str, float]) -> List[Dict[str, Any]]:
    defs = [
        ("density", "人群密度", "人/m2", "继续下降"),
        ("speed", "平均速度", "m/s", "继续提升"),
        ("pressure", "压力指数", "N", "压力下降"),
        ("riskIndex", "综合风险指数", "", "风险下降"),
        ("congestionMinutes", "拥堵持续时间", "min", "持续缩短"),
    ]
    rows = []
    for key, label, unit, direction in defs:
        rows.append({
            "key": key,
            "label": label,
            "unit": unit,
            "current": current[key],
            "target": target[key],
            "direction": direction,
        })
    return rows


def _process_steps(
    profile: str,
    phase: str,
    angle: str,
    weakness: str,
    upper: Dict[str, Any],
    lower: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    priorities = " > ".join(upper["regionLabels"].get(item, item) for item in upper["regionPriorities"])
    action_summary = "；".join(f"{item['region']}：{item['technique']}" for item in lower)
    return [
        {
            "stage": "态势识别",
            "title": f"判定为{phase}阶段",
            "desc": f"primaryWeakness 指向「{weakness or '综合应急响应'}」，优化角度为 {angle}，场景剖面 {profile}。",
            "calculation": "density/riskIndex/floodDepth → emergencyPhase",
            "output": f"emergencyPhase={phase}",
        },
        {
            "stage": "上层统筹",
            "title": "区域优先级与资源集中",
            "desc": f"按主风险集中投放，而非均衡分配。当前排序：{priorities}。",
            "calculation": f"max 风险下降 - 资源成本｜警力合计 {TOTAL_OFFICERS}",
            "output": f"regionPriorities={upper['regionPriorities']}",
        },
        {
            "stage": "下层执行",
            "title": "分区动作组合",
            "desc": action_summary,
            "calculation": "u*_e = argmin(R_e + D_e + spillover惩罚)",
            "output": "regionalActions[...]",
        },
        {
            "stage": "升级规则",
            "title": "阶段切换条件写入方案",
            "desc": "；".join(rule["condition"] + " → " + rule["action"] for rule in upper["phaseSwitchRules"][:2]),
            "calculation": "ΔR实际 → 更新 λ_e、b_e → 再优化",
            "output": "phaseSwitchRules[...]",
        },
    ]


def _ui_actions(lower: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for index, item in enumerate(lower, start=1):
        rows.append({
            "index": f"{index:02d}",
            "title": item["region"],
            "region": item["region"],
            "regionId": item["regionId"],
            "decision": item["decision"],
            "problem": item["problem"],
            "technique": item["technique"],
            "actions": item["actions"],
            "method": f"{item['technique']}；动作：{'、'.join(item['actions'])}",
            "effect": f"预期压低该区域风险并抑制向外围转移",
            "expectedEffect": "局部风险下降，spillover 受约束",
        })
    return rows


def _execution_plan(phase: str, lower: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    first = "；".join(f"{item['region']}{item['technique']}" for item in lower[:3])
    return [
        {"type": "T+0", "title": "立即执行分区组合处置", "desc": first or "按上层优先级启动现场动作。"},
        {"type": "T+2min", "title": "滚动反馈与升级判定", "desc": "主风险区下降而外围上升时，加码廊道/绕行限流，不单独给热点减压。"},
        {"type": "T+5min", "title": f"阶段复核（当前 {phase}）", "desc": "未达密度降幅阈值则升级硬控；连续改善则转入 recovery 平滑退出。"},
    ]


def build_arde_optimization(data: Dict[str, Any], simulator: Any = None) -> Dict[str, Any]:
    """Build an OptimizationPlan-compatible ARDE result.

    Optional *simulator* lets the optimizer overlay live frame metrics when the
    WebSocket evaluation backend is already running.
    """
    request_id = str(data.get("requestId") or "")
    evaluation = data.get("evaluation") if isinstance(data.get("evaluation"), dict) else {}
    workflow = data.get("workflow") if isinstance(data.get("workflow"), dict) else {}
    experiment = data.get("experimentPayload") if isinstance(data.get("experimentPayload"), dict) else {}
    options = data.get("options") if isinstance(data.get("options"), dict) else {}

    if simulator is not None:
        try:
            frame = simulator.frame()
            live = frame.get("metrics") or {}
            if live and not experiment.get("baselineMetrics"):
                experiment = {
                    **experiment,
                    "baselineMetrics": {
                        "density": _number(live.get("density"), 0.0) or evaluation.get("density") or 0.0,
                        "speed": _number(live.get("avg_speed"), 0.72),
                        "pressure": _number(live.get("congestion"), 0.0) * 60.0,
                        "riskIndex": _number(live.get("congestion"), 0.0) * 80.0,
                        "congestionMinutes": _number(live.get("congestion"), 0.0) * 20.0,
                    },
                }
        except Exception:
            pass

    profile = infer_emergency_profile(evaluation, workflow, options)
    weakness = str(evaluation.get("primaryWeakness") or "")
    angle = infer_weakness_angle(weakness)
    metrics = _metrics_from_payload(evaluation, workflow, experiment)
    flood_depth = _flood_depth(evaluation, workflow, experiment)
    final_score = _number(evaluation.get("finalScore"), 78.0)
    phase = infer_phase(metrics, flood_depth, final_score)
    upper = _upper_layer(profile, phase, angle, metrics, flood_depth)
    lower = _lower_actions(profile, phase, angle, upper)
    aggregated = _aggregate_policy(profile, phase, angle, flood_depth)
    expected = _expected_metrics(metrics, phase, angle, profile)
    score = int(_clamp(final_score + (12 if phase == "response" else 6), 60.0, 96.0))

    option_title = {
        "crowd_crush": "响应阶段组合处置：截流 + 单向 + 防转移",
        "urban_flood_720": "响应阶段组合处置：先封、再排、再导",
        "combined": "复合响应：热点管控与积水封排同步",
    }.get(profile, "双层应急组合处置")
    if phase == "warning":
        option_title = "预警阶段：岗位预置与轻度限流"
    elif phase == "recovery":
        option_title = "恢复阶段：平滑降级并防止回流"

    workflow_id = str(data.get("workflowId") or workflow.get("id") or evaluation.get("workflowId") or "")
    result = {
        "ok": True,
        "type": "arde_optimization_result",
        "requestId": request_id,
        "engine": ENGINE_ID,
        "score": score,
        "option": option_title,
        "emergencyPhase": phase,
        "targets": [
            {"label": "峰值密度", "current": metrics["density"], "target": expected["density"], "unit": "人/m²"},
            {"label": "综合风险", "current": metrics["riskIndex"], "target": expected["riskIndex"], "unit": "指数"},
            {"label": "拥堵时长", "current": metrics["congestionMinutes"], "target": expected["congestionMinutes"], "unit": "min"},
        ],
        "expectedMetrics": expected,
        "expectedMetricRows": _ui_metric_rows(metrics, expected),
        "process": _process_steps(profile, phase, angle, weakness, upper, lower),
        "actions": _ui_actions(lower),
        "execution": _execution_plan(phase, lower),
        "checklist": [
            "确认上层全局目标",
            "确认区域资源预算",
            "确认下层动作边界",
            "确认相邻区域协同约束",
            "确认反馈回传周期",
        ],
        "method": {
            "id": METHOD_ID,
            "name": METHOD_NAME,
            "type": "分层协同",
        },
        "arde": {
            "engine": ENGINE_ID,
            "emergencyPhase": phase,
            "upperLayer": {
                "profile": profile,
                "regionPriorities": upper["regionPriorities"],
                "regionLabels": upper["regionLabels"],
                "resourceAllocation": upper["resourceAllocation"],
                "phaseSwitchRules": upper["phaseSwitchRules"],
            },
            "lowerLayer": {
                "regionalActions": [
                    {
                        "regionId": item["regionId"],
                        "region": item["region"],
                        "decision": item["decision"],
                        "actions": item["actions"],
                        "problem": item["problem"],
                        "technique": item["technique"],
                        "params": item["params"],
                    }
                    for item in lower
                ]
            },
            "aggregatedPolicy": aggregated,
        },
        "generatedAt": _now_iso(),
        "workflowId": workflow_id,
    }
    return result


if __name__ == "__main__":
    import json
    import sys

    raw = sys.stdin.read().strip() or "{}"
    print(json.dumps(build_arde_optimization(json.loads(raw)), ensure_ascii=False, indent=2))
