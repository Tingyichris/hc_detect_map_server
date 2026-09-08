from hcdmap_server.fusion.travel_time import TravelTimeModel


def test_seed_then_learn():
    m = TravelTimeModel()
    m.seed_from_map([("a", "b", 0.25)], approx_loop_seconds=100)
    est = m.get("a", "b")
    assert est.estimate() == 25.0  # seed = loop * gap

    for _ in range(10):
        est.observe(12.0)
    assert abs(est.estimate() - 12.0) < 0.01  # learned value overrides the seed


def test_match_window_widens_when_undersampled():
    m = TravelTimeModel()
    m.seed_from_map([("a", "b", 0.5)], approx_loop_seconds=60)
    est = m.get("a", "b")
    lo1, hi1 = est.match_window(2.0)
    for _ in range(20):
        est.observe(30.0)
    lo2, hi2 = est.match_window(2.0)
    assert (hi2 - lo2) <= (hi1 - lo1)
