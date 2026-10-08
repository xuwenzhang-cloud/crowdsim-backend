"""Effectiveness demo: same CrowdSim scene, different policies.

Records every micro-step ``t`` (and macro window ``T``) so the frontend can
replay crowd + system state changes — not only the final snapshot.

    cd crowdsim-backend
    python -m arde_emergency.demo_effectiveness
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .optimizer import build_arde_optimization  # noqa: E402

# Macro windows shown on the UI (mapped onto the control horizon).
MACRO_WINDOWS = [
    {"T": 0, "label": "T+0 立即执行", "system_focus": "启动分区组合处置 / 下发策略参数"},
    {"T": 1, "label": "T+2min 滚动反馈", "system_focus": "外围上升则加码廊道限流，热点不单独减压"},
    {"T": 2, "label": "T+5min 阶段复核", "system_focus": "未达降幅则升级硬控；持续改善则准备 recovery"},
]


def _load_overlay_module():
    backend_dir = ROOT / "MACE-FE2-main" / "frontend" / "src" / "views" / "crowdSim" / "backend"
    bundled = backend_dir / "crowdsim_overlay_server.py"
    backend_str = str(backend_dir)
    if backend_str not in sys.path:
        sys.path.insert(0, backend_str)
    spec = importlib.util.spec_from_file_location("crowdsim_overlay_server_bundled", bundled)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load overlay server from {bundled}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _scenario_paths() -> Tuple[str, str, str]:
    candidates = [
        ROOT / "scenarios" / "shanghai_bund",
    ]
    for folder in candidates:
        net = folder / "bund.net.xml"
        if net.exists():
            return (
                str(net),
                str(folder / "bund_ped.rou.xml"),
                str(folder / "bund_veh.rou.xml"),
            )
    raise FileNotFoundError("shanghai_bund scenario not found under platform/")


def _policy_knobs(sim: Any) -> Dict[str, Any]:
    policy = getattr(sim, "policy", None) or {}
    if not isinstance(policy, dict):
        return {
            "name": "",
            "strength": 0.0,
            "diversion": 0.0,
            "capacity_multiplier": 1.0,
            "water_decay_per_second": 0.0,
            "speed_recovery": 0.0,
        }
    return {
        "name": str(policy.get("name") or ""),
        "strength": round(float(policy.get("strength") or 0.0), 4),
        "diversion": round(float(policy.get("diversion") or 0.0), 4),
        "capacity_multiplier": round(float(policy.get("capacity_multiplier") or 1.0), 4),
        "water_decay_per_second": round(float(policy.get("water_decay_per_second") or 0.0), 4),
        "speed_recovery": round(float(policy.get("speed_recovery") or 0.0), 4),
    }


def _snapshot(sim: Any) -> Dict[str, Any]:
    frame = sim.frame()
    metrics = frame.get("metrics") or {}
    region = frame.get("event_region_metrics") or {}
    event = (frame.get("event_state") or {}).get("active_event") or {}
    water_depth = 0.0
    water_radius = 0.0
    if getattr(sim, "flood_zones", None):
        water_depth = sum(float(zone.depth) for zone in sim.flood_zones) / max(1, len(sim.flood_zones))
        water_radius = sum(float(zone.radius) for zone in sim.flood_zones) / max(1, len(sim.flood_zones))
    affected = float(metrics.get("affected") or 0.0)
    pedestrians = float(len(frame.get("pedestrians") or []))
    congestion = float(metrics.get("congestion") or 0.0)
    avg_speed = float(metrics.get("avg_speed") or 0.0)
    event_intensity = float(event.get("intensity") or 0.0)
    knobs = _policy_knobs(sim)
    return {
        "avg_speed": round(avg_speed, 4),
        "congestion": round(congestion, 4),
        "affected": affected,
        "pedestrians": pedestrians,
        "region_affected": float(region.get("affected_pedestrians") or region.get("affected") or 0.0),
        "evacuation_rate": float(region.get("evacuation_rate") or 0.0),
        "event_intensity": round(event_intensity, 4),
        "event_phase": str(event.get("phase") or ""),
        "water_depth": round(water_depth, 4),
        "water_radius": round(water_radius, 1),
        "risk_proxy": round(
            congestion * 55.0
            + event_intensity * 22.0
            + min(45.0, affected * 0.025)
            + min(25.0, pedestrians * 0.004),
            2,
        ),
        "policy": knobs["name"],
        "system": knobs,
    }


def _macro_index(control_t: int, horizon: int) -> int:
    """Map control micro-step into macro window T = 0/1/2."""
    if horizon <= 0:
        return 0
    third = max(1, (horizon + 2) // 3)
    if control_t <= third:
        return 0
    if control_t <= third * 2:
        return 1
    return 2


def _run_control_with_timeline(
    sim: Any,
    horizon: int,
    stage: str,
    t0: int,
    control_horizon: int = 0,
) -> List[Dict[str, Any]]:
    """Advance steps and record crowd + system state each micro-step t."""
    series: List[Dict[str, Any]] = []
    step_length = float(getattr(sim, "step_length", 0.5) or 0.5)
    for i in range(max(0, horizon)):
        sim.step()
        global_t = t0 + i + 1
        snap = _snapshot(sim)
        if stage == "control":
            control_t = i + 1
            T = _macro_index(control_t, control_horizon or horizon)
            macro = MACRO_WINDOWS[T]
        else:
            control_t = 0
            T = -1
            macro = {"label": "尚未进入调控窗口", "system_focus": "等待事件发展 / 尚未下发策略"}
        series.append({
            "t": global_t,
            "control_t": control_t,
            "sim_time": round(global_t * step_length, 2),
            "stage": stage,
            "T": T,
            "T_label": macro["label"],
            "system_focus": macro["system_focus"],
            "crowd": {
                "avg_speed": snap["avg_speed"],
                "congestion": snap["congestion"],
                "affected": snap["affected"],
                "pedestrians": snap["pedestrians"],
                "region_affected": snap["region_affected"],
                "evacuation_rate": snap["evacuation_rate"],
                "risk_proxy": snap["risk_proxy"],
            },
            "system": {
                "policy": snap["policy"],
                "event_phase": snap["event_phase"],
                "event_intensity": snap["event_intensity"],
                "water_depth": snap["water_depth"],
                "water_radius": snap["water_radius"],
                "strength": snap["system"]["strength"],
                "diversion": snap["system"]["diversion"],
                "capacity_multiplier": snap["system"]["capacity_multiplier"],
                "water_decay_per_second": snap["system"]["water_decay_per_second"],
                "speed_recovery": snap["system"]["speed_recovery"],
                "focus": macro["system_focus"],
            },
            "avg_speed": snap["avg_speed"],
            "congestion": snap["congestion"],
            "affected": snap["affected"],
            "pedestrians": snap["pedestrians"],
            "evacuation_rate": snap["evacuation_rate"],
            "risk_proxy": snap["risk_proxy"],
            "event_intensity": snap["event_intensity"],
            "water_depth": snap["water_depth"],
            "policy": snap["policy"],
            "event_phase": snap["event_phase"],
        })
    return series


def _setup_incident(sim: Any) -> None:
    lat, lon = sim.center
    sim.trigger_event({
        "eventId": "demo-combined",
        "eventType": "crowd_surge",
        "center": {"lat": lat, "lng": lon},
        "radius": 280.0,
        "intensity": 0.82,
        "duration": 180.0,
    })
    sim.set_flood({
        "mode": "static_points",
        "points": [
            {"lat": lat, "lng": lon, "depth": 0.28, "radius": 160.0},
        ],
    })


def _arde_policy() -> Dict[str, Any]:
    sample = json.loads((Path(__file__).parent / "examples" / "sample_optimize_request.json").read_text(encoding="utf-8"))
    result = build_arde_optimization(sample)
    aggregated = result["arde"]["aggregatedPolicy"]
    return {
        "decision": aggregated["decision"],
        "payload": aggregated.get("payload") or {},
        "label": f"ARDE {aggregated['decision']}",
        "plan": result,
        "execution": result.get("execution") or [],
    }


def _arms() -> List[Dict[str, Any]]:
    arde = _arde_policy()
    return [
        {"id": "none", "label": "无干预", "decision": None, "payload": {}, "execution": []},
        {"id": "observe_only", "label": "仅观察", "decision": "observe_only", "payload": {}, "execution": []},
        {
            "id": "police_guidance",
            "label": "警力引导（系统原 preset）",
            "decision": "police_guidance",
            "payload": {"containmentLevel": 70},
            "execution": [],
        },
        {
            "id": "temporary_diversion",
            "label": "临时分流（系统原 preset）",
            "decision": "temporary_diversion",
            "payload": {},
            "execution": [],
        },
        {
            "id": "arde",
            "label": arde["label"],
            "decision": arde["decision"],
            "payload": arde["payload"],
            "execution": arde["execution"],
            "plan": arde["plan"],
        },
    ]


def run_demo(warmup: int, delay: int, horizon: int, population: int) -> Dict[str, Any]:
    overlay = _load_overlay_module()
    net, ped, veh = _scenario_paths()
    print(f"[demo] loading network {net}", flush=True)
    started = time.time()
    sim = overlay.OverlayNetworkSimulator(net, ped, veh)
    sim.configure(count=population, speed_factor=1.0)
    print(f"[demo] simulator ready in {time.time() - started:.1f}s, agents≈{len(sim.agents)}", flush=True)

    results: List[Dict[str, Any]] = []
    for arm in _arms():
        print(f"[demo] arm={arm['id']} ...", flush=True)
        sim.reset()
        sim.configure(count=population, speed_factor=1.0)

        pre_incident = _run_control_with_timeline(sim, warmup, stage="warmup", t0=0)
        _setup_incident(sim)
        pre_policy = _run_control_with_timeline(sim, delay, stage="incident_open", t0=warmup)
        before = _snapshot(sim)
        if arm["decision"]:
            sim.apply_policy({"decision": arm["decision"], "payload": arm["payload"]})
        control = _run_control_with_timeline(
            sim, horizon, stage="control", t0=warmup + delay, control_horizon=horizon
        )
        after = control[-1] if control else before

        timeline = pre_incident + pre_policy + control
        crowd_after = after.get("crowd") if isinstance(after, dict) else None
        system_after = after.get("system") if isinstance(after, dict) else None
        after_flat = {
            "avg_speed": after.get("avg_speed", before["avg_speed"]),
            "congestion": after.get("congestion", before["congestion"]),
            "affected": after.get("affected", before["affected"]),
            "pedestrians": after.get("pedestrians", before["pedestrians"]),
            "region_affected": (crowd_after or {}).get("region_affected", before.get("region_affected", 0)),
            "evacuation_rate": after.get("evacuation_rate", before["evacuation_rate"]),
            "event_intensity": after.get("event_intensity", before["event_intensity"]),
            "event_phase": after.get("event_phase", before["event_phase"]),
            "water_depth": after.get("water_depth", before["water_depth"]),
            "water_radius": (system_after or {}).get("water_radius", before.get("water_radius", 0)),
            "risk_proxy": after.get("risk_proxy", before["risk_proxy"]),
            "policy": after.get("policy", before["policy"]),
        }
        results.append({
            "id": arm["id"],
            "label": arm["label"],
            "decision": arm["decision"] or "",
            "before": before,
            "after": after_flat,
            "delta": {
                "avg_speed": round(after_flat["avg_speed"] - before["avg_speed"], 4),
                "congestion": round(after_flat["congestion"] - before["congestion"], 4),
                "affected": after_flat["affected"] - before["affected"],
                "event_intensity": round(after_flat["event_intensity"] - before["event_intensity"], 4),
                "water_depth": round(after_flat["water_depth"] - before["water_depth"], 4),
                "risk_proxy": round(after_flat["risk_proxy"] - before["risk_proxy"], 2),
                "evacuation_rate": round(after_flat["evacuation_rate"] - before["evacuation_rate"], 4),
            },
            "timeline": timeline,
            "execution": arm.get("execution") or [],
            "markers": {
                "incident_at_t": warmup,
                "policy_at_t": warmup + delay,
                "end_t": warmup + delay + horizon,
            },
        })
        print(
            f"         risk {before['risk_proxy']} → {after_flat['risk_proxy']}  "
            f"speed {before['avg_speed']} → {after_flat['avg_speed']}  "
            f"water {before['water_depth']} → {after_flat['water_depth']}  "
            f"policy={after_flat['policy']}  timeline={len(timeline)}",
            flush=True,
        )

    baseline_risk = next(item["after"]["risk_proxy"] for item in results if item["id"] == "none")
    arde_row = next(item for item in results if item["id"] == "arde")
    best_legacy = min(
        (item for item in results if item["id"] in {"police_guidance", "temporary_diversion"}),
        key=lambda item: item["after"]["risk_proxy"],
    )
    summary = {
        "arde_beats_no_intervention": arde_row["after"]["risk_proxy"] < baseline_risk,
        "arde_beats_best_legacy_preset": arde_row["after"]["risk_proxy"] <= best_legacy["after"]["risk_proxy"],
        "baseline_risk": baseline_risk,
        "arde_risk": arde_row["after"]["risk_proxy"],
        "best_legacy": best_legacy["id"],
        "best_legacy_risk": best_legacy["after"]["risk_proxy"],
        "arde_policy": arde_row["decision"],
    }
    return {
        "ok": True,
        "warmup": warmup,
        "delay": delay,
        "horizon": horizon,
        "population": population,
        "step_length": sim.step_length,
        "macro_windows": MACRO_WINDOWS,
        "arms": results,
        "summary": summary,
        "elapsed_sec": round(time.time() - started, 1),
        "has_timeline": True,
    }


def _print_table(payload: Dict[str, Any]) -> None:
    print("\n=== CrowdSim × ARDE 有效性小实验（含 t/T 时序） ===")
    print(
        f"warmup={payload['warmup']} delay={payload['delay']} "
        f"horizon={payload['horizon']}  (step={payload['step_length']}s)"
    )
    print(
        f"{'策略':<28} {'风险↓':>8} {'速度↑':>8} {'拥堵↓':>8} "
        f"{'事件强度↓':>10} {'水深↓':>8} {'疏散率↑':>8} {'时序点':>6}"
    )
    for arm in payload["arms"]:
        after = arm["after"]
        print(
            f"{arm['label']:<28} {after['risk_proxy']:>8.2f} {after['avg_speed']:>8.3f} "
            f"{after['congestion']:>8.3f} {after['event_intensity']:>10.3f} "
            f"{after['water_depth']:>8.3f} {after['evacuation_rate']:>8.3f} "
            f"{len(arm.get('timeline') or []):>6}"
        )
    summary = payload["summary"]
    print("\n判定：")
    print(
        f"  ARDE 优于无干预: {'是' if summary['arde_beats_no_intervention'] else '否'}  "
        f"({summary['arde_risk']} vs {summary['baseline_risk']})"
    )
    print(
        f"  ARDE 不差于最好的原 preset（{summary['best_legacy']}）: "
        f"{'是' if summary['arde_beats_best_legacy_preset'] else '否'}  "
        f"({summary['arde_risk']} vs {summary['best_legacy_risk']})"
    )
    print(f"  下发 decision: {summary['arde_policy']}")
    print(f"  用时 {payload['elapsed_sec']}s")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare ARDE policy against CrowdSim built-in presets.")
    parser.add_argument("--warmup", type=int, default=8)
    parser.add_argument("--delay", type=int, default=6)
    parser.add_argument("--horizon", type=int, default=48)
    parser.add_argument("--population", type=int, default=900)
    parser.add_argument(
        "--out",
        default=str(ROOT / "arde_emergency" / "examples" / "demo_effectiveness_result.json"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = run_demo(args.warmup, args.delay, args.horizon, args.population)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_table(payload)
    print(f"\n结果已写入 {args.out}")


if __name__ == "__main__":
    main()
