import math

from hcdmap_server.map.geometry import TrackGeometry, build_segments


def _square():
    return TrackGeometry.build([(0, 0), (10, 0), (10, 10), (0, 10)], closed=True)


def test_closed_loop_length_and_roundtrip():
    g = _square()
    assert math.isclose(g.length_px, 40.0, rel_tol=0.05)
    # s=0 is the first point; s=0.5 is halfway around.
    p0 = g.point_at(0.0)
    p_half = g.point_at(0.5)
    assert math.dist(p0, p_half) > 5  # opposite side of the square-ish loop


def test_project_is_inverse_of_point_at():
    g = _square()
    for s in (0.1, 0.25, 0.4, 0.75, 0.9):
        px = g.point_at(s)
        assert abs(g.project(px) - s) < 0.02


def test_forward_gap_wraps():
    g = _square()
    assert math.isclose(g.forward_gap(0.9, 0.1), 0.2, abs_tol=1e-6)
    assert math.isclose(g.forward_gap(0.1, 0.9), 0.8, abs_tol=1e-6)


def test_build_segments_orders_around_loop():
    segs = build_segments([("b", 0.6), ("a", 0.2), ("c", 0.9)])
    assert [s[0] for s in segs] == ["a", "b", "c"]
    assert segs[-1][1] == "a"  # wraps back to the first
