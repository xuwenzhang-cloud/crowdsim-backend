from crowdsim.control.inflow_meter import InflowMeter


def test_inflow_meter_queues_then_releases_at_configured_rate():
    meter = InflowMeter("a", "entry", rate=0.5, nominal_capacity_per_second=2.0, last_time=0.0)
    held, released = meter.regulate({"p1", "p2", "p3"}, 0.0)
    assert released == {"p1"}
    assert held == {"p2", "p3"}
    held, released = meter.regulate({"p1", "p2", "p3"}, 1.0)
    assert released == {"p2"}
    assert held == {"p3"}


def test_full_rate_never_holds_pedestrians():
    meter = InflowMeter("a", "entry", rate=1.0, nominal_capacity_per_second=1.0, last_time=0.0)
    held, released = meter.regulate({"p1", "p2"}, 0.0)
    assert held == set()
    assert released == {"p1", "p2"}
