"""Data contracts shared by the SUMO runtime modules."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CrowdEvent:
    id: str
    x: float
    y: float
    radius: float
    intensity: float
    density_multiplier: float
    expires_at: float
    event_type: str = "generic"
    speed_impact: float = 0.35
    stress_impact: float = 0.6
    avoidance_pressure: float = 0.5
    source: str = "event"
    started_at: float = 0.0
    peak_intensity: float = 1.0
    growth_seconds: float = 0.0
    decay_seconds: float = 0.0
    phase: str = "active"


@dataclass(frozen=True)
class AgentProfile:
    person_id: str
    free_walking_speed: float = 1.35
    mobility: float = 1.0
    perception_radius: float = 12.0
    familiarity: float = 0.5
    risk_tolerance: float = 0.5
    patience: float = 0.5
    crowding_tolerance: float = 0.5
    following_tendency: float = 0.5
    authority_compliance: float = 0.5
    information_trust: Dict[str, float] = field(default_factory=dict)
    group_cohesion: float = 0.5
    stress_susceptibility: float = 0.5
    recovery_seconds: float = 60.0
    endurance: float = 0.5
    age_group: str = "adult"
    occupation: str = "unspecified"
    nationality: str = "unspecified"
    native_language: str = "unspecified"
    visit_purpose: str = "pass_through"
    crowd_role: str = "unspecified"
    age_band: str = "unspecified"
    gender: str = "unspecified"
    origin: str = "unspecified"


@dataclass(frozen=True)
class MotionSnapshot:
    person_id: str
    time_seconds: float
    x: float
    y: float
    lon: float
    lat: float
    speed: float
    edge_id: str
    lane_id: str
    lane_position: float
    angle: float
    stage_index: int
    stage_type: int
    departed: bool = False
    remaining_stage_count: int = 1


@dataclass
class AgentState:
    person_id: str
    stress: float = 0.0
    fatigue: float = 0.0
    perceived_risk: float = 0.0
    perceived_crowding: float = 0.0
    blocked_duration: float = 0.0
    planned_wait_until: Optional[float] = None
    activity_state: str = "walking"
    known_events: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    received_messages: List[str] = field(default_factory=list)
    current_goal: Optional[str] = None
    activity_plan: List[str] = field(default_factory=list)
    time_budget: Optional[float] = None
    group_id: Optional[str] = None
    companion_ids: List[str] = field(default_factory=list)
    rendezvous_id: Optional[str] = None
    current_plan: Optional["BehaviorPlan"] = None
    next_decision_time: float = 0.0
    poi_initialized: bool = False
    poi_plan_active: bool = False
    pending_goal: Optional[str] = None
    hotspot_entry_edge: Optional[str] = None
    hotspot_last_route_change_time: Optional[float] = None
    hotspot_next_route_check: float = 0.0
    hotspot_dwell_until: Optional[float] = None
    hotspot_activity_phase: Optional[str] = None
    # Display evidence is separate from blocked_duration used by behavior rules.
    low_speed_duration: float = 0.0
    critical_density_duration: float = 0.0
    dense_low_speed_duration: float = 0.0
    visual_blocked_duration: float = 0.0


@dataclass(frozen=True)
class Observation:
    person_id: str
    snapshot_id: str
    time_seconds: float
    own_motion: MotionSnapshot
    neighbour_ids: Tuple[str, ...] = ()
    local_people_count: int = 0
    local_area_m2: Optional[float] = None
    objective_density_per_m2: Optional[float] = None
    perceived_crowding: float = 0.0
    perceived_risk: float = 0.0
    density_level: str = "unknown"
    flood_impact: float = 0.0
    event_impact: float = 0.0
    known_event_ids: Tuple[str, ...] = ()
    available_goal_ids: Tuple[str, ...] = ()


@dataclass(frozen=True)
class BehaviorPlan:
    person_id: str
    snapshot_id: str
    proposed_action: str
    target_id: Optional[str] = None
    route_edges: Tuple[str, ...] = ()
    speed_limit: Optional[float] = None
    wait_until: Optional[float] = None
    activity_duration: Optional[float] = None
    next_route_edges: Tuple[str, ...] = ()
    reason: str = ""
    confidence: Optional[float] = None
    source: str = "rule"
    decided_at: float = 0.0
    expires_at: Optional[float] = None
    arrival_position: Optional[float] = None
    next_arrival_position: Optional[float] = None
    next_target_id: Optional[str] = None
    selected_entry_edge: Optional[str] = None
    preserve_future_stages: bool = False


@dataclass(frozen=True)
class PlanExecutionResult:
    person_id: str
    proposed_action: str
    applied_action: str
    status: str
    applied_at: float
    reason: str = ""


@dataclass
class GroupRecord:
    group_id: str
    member_ids: List[str]
    leader_id: Optional[str] = None
    rendezvous_id: Optional[str] = None
