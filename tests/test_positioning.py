"""Regression tests for the positioning maths, run against the real `bps`
module (Home Assistant stubbed by conftest). Covers the pieces most likely to
regress silently: the trilateration solver, receiver mount-height slant
correction, 3D calibration ground truth, and the floor hypothesis-competition
election helpers.
"""
import asyncio
import math

import bps
from bps import calibration as cal_mod
from conftest import make_hass

SCALE = 40.0  # px per metre


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# Trilateration solver
# --------------------------------------------------------------------------- #
def test_trilaterate_recovers_known_point():
    d = math.hypot(5, 5)
    pts = [(0, 0, d), (10, 0, d), (0, 10, d)]
    x, y = bps.trilaterate(pts)
    assert abs(x - 5) < 0.05 and abs(y - 5) < 0.05


def test_trilaterate_zero_radius_survives():
    # Regression: a 0 radius must not divide-by-zero and abort the solve.
    res = bps.trilaterate([(0, 0, 0.0), (10, 0, 10.0), (0, 10, 10.0)])
    assert res is not None


def test_min_weight_radius_tames_a_spuriously_short_reading():
    truth = (140.0, 140.0)
    far = [
        (400.0, 140.0, 260.0),
        (140.0, 400.0, 260.0),
        (400.0, 400.0, math.hypot(260, 260)),
    ]
    liar = (100.0, 100.0, 0.01)          # claims the tracker is basically on it
    honest = (140.0, 140.0, 20.0)        # 0.5 m at 40 px/m, corroborated
    pts = far + [liar, honest]
    d_unclamped = math.dist(bps.trilaterate(pts), truth)
    d_clamped = math.dist(bps.trilaterate(pts, min_weight_radius=20.0), truth)
    assert d_clamped < d_unclamped - 15.0  # the clamp pulls the fit off the liar


# --------------------------------------------------------------------------- #
# Tracker height + slant correction
# --------------------------------------------------------------------------- #
def test_tracker_height_default_and_override():
    assert bps._tracker_height({}) == bps.TRACKER_HEIGHT_M
    assert bps._tracker_height({"tracker_height": 0.3}) == 0.3
    assert bps._tracker_height({"tracker_height": 99}) == bps.TRACKER_HEIGHT_M  # out of range


def _run_radii(state, unit="m", height=None, tracker_height=None):
    class St:
        def __init__(self):
            self.state = state
            self.attributes = {"unit_of_measurement": unit}

    class Hass:
        states = type("S", (), {"get": staticmethod(lambda _eid: St())})()

    rec = {"entity_id": "probe", "cords": {"x": 0, "y": 0}}
    if height is not None:
        rec["height"] = height
    data = {"floor": [{"name": "F", "scale": SCALE, "receivers": [rec]}]}
    if tracker_height is not None:
        data["tracker_height"] = tracker_height
    run(bps.update_receiver_radii(Hass(), {"entity": "phone", "data": data}))
    return rec


def test_no_height_leaves_distance_alone():
    r = _run_radii("2.3")
    assert abs(r["distance"] - 2.3) < 1e-9
    assert abs(r["cords"]["r"] - 92.0) < 1e-6


def test_height_removes_vertical_leg_from_radius_only():
    # dz = 1.2 -> horizontal sqrt(2.3^2 - 1.2^2) = 1.962 m into the radius;
    # the election "distance" stays the calibrated slant (cross-floor safe).
    r = _run_radii("2.3", height=2.2)
    assert abs(r["cords"]["r"] - 1.9621 * SCALE) < 0.1
    assert abs(r["distance"] - 2.3) < 1e-9


def test_height_underneath_clamps_radius_to_zero():
    r = _run_radii("1.0", height=2.2)  # slant < vertical leg
    assert r["cords"]["r"] == 0.0


def test_nan_and_out_of_range_height_ignored():
    assert abs(_run_radii("2.3", height=float("nan"))["cords"]["r"] - 92.0) < 1e-6
    assert abs(_run_radii("2.3", height=float("inf"))["cords"]["r"] - 92.0) < 1e-6
    assert abs(_run_radii("2.3", height=25)["cords"]["r"] - 92.0) < 1e-6


# --------------------------------------------------------------------------- #
# Calibration ground truth (2D vs 3D)
# --------------------------------------------------------------------------- #
def _cal(ha=None, hb=None):
    return {"receivers": {
        "a": {"x": 0.0, "y": 0.0, "scale": SCALE, "floor": "F", "height": ha},
        "b": {"x": 120.0, "y": 0.0, "scale": SCALE, "floor": "F", "height": hb},
    }}


def test_true_distance_2d_when_heights_absent_or_partial():
    assert abs(cal_mod._true_distance_m(_cal(), "a", "b") - 3.0) < 1e-9
    assert abs(cal_mod._true_distance_m(_cal(ha=2.2), "a", "b") - 3.0) < 1e-9


def test_true_distance_3d_when_both_heights_present():
    # 3 m apart on the map, 0.3 m vs 2.2 m high -> sqrt(9 + 1.9^2) = 3.551 m.
    assert abs(cal_mod._true_distance_m(_cal(ha=0.3, hb=2.2), "a", "b") - 3.5511) < 1e-3


# --------------------------------------------------------------------------- #
# Floor election helpers
# --------------------------------------------------------------------------- #
def test_score_rewards_agreement_and_coverage():
    good = [(x, y, math.hypot(x - 400, y - 400), 1.0)
            for (x, y) in [(0, 0), (800, 0), (0, 800), (800, 800), (400, 0)]]
    conf, rms, cov = bps._score_floor_fit((400.0, 400.0), good, SCALE)
    assert rms < 1e-6 and cov == 1.0 and conf > 0.99

    bad = [(0, 0, 40.0, 1.0), (800, 0, 40.0, 1.0), (0, 800, 40.0, 1.0)]
    conf_bad, rms_bad, _ = bps._score_floor_fit((400.0, 400.0), bad, SCALE)
    assert rms_bad > 5.0 and conf_bad < conf


def test_probabilities_converge_and_drop_renamed():
    bps._floor_probability.clear()
    for _ in range(30):
        probs = bps._update_floor_probabilities("e", {"a": 0.8, "b": 0.2})
    assert abs(probs["a"] - 0.8) < 0.02
    probs = bps._update_floor_probabilities("e", {"c": 1.0}, valid_floors={"c"})
    assert "a" not in probs and "b" not in probs


def test_elect_hysteresis_and_dwell():
    # No incumbent: adopt the best immediately.
    floor, ch = bps._elect_floor({"a": 0.6, "b": 0.4}, None, {"a", "b"}, None)
    assert floor == "a" and ch is None
    # Incumbent holds within the margin.
    floor, _ = bps._elect_floor({"a": 0.52, "b": 0.48}, "b", {"a", "b"}, None)
    assert floor == "b"
    # A leading challenger must persist FLOOR_SWITCH_CYCLES before switching.
    ch = None
    seen = []
    for _ in range(4):
        floor, ch = bps._elect_floor({"a": 0.7, "b": 0.3}, "b", {"a", "b"}, ch)
        seen.append(floor)
    assert seen[:bps.FLOOR_SWITCH_CYCLES] == ["b"] * (bps.FLOOR_SWITCH_CYCLES - 1) + ["a"]


# --------------------------------------------------------------------------- #
# Receiver leave-one-out self-localization (run_selftest)
# --------------------------------------------------------------------------- #
def _hass_with(receivers, samples, scale=SCALE, floor="F"):
    """Fake hass carrying a BPS layout + calibration samples for run_selftest."""
    hass = make_hass()
    recs = []
    for r in receivers:
        d = {"entity_id": r[0], "cords": {"x": r[1], "y": r[2]}}
        if len(r) > 3 and r[3] is not None:
            d["height"] = r[3]
        recs.append(d)
    hass.data.setdefault("bps", {})["layout"] = {
        "floor": [{"name": floor, "scale": scale, "receivers": recs}]
    }
    hass.data["bps"]["calibration"] = {"samples": samples}
    return hass


def _exact_samples(receivers, scale=SCALE):
    """samples["target|rx"] = the true slant distance, so a faithful solve
    recovers each receiver exactly (heights, when set, are corrected back out)."""
    pos = {r[0]: (r[1], r[2], (r[3] if len(r) > 3 else None)) for r in receivers}
    s = {}
    for a in pos:
        for b in pos:
            if a == b:
                continue
            ax, ay, ah = pos[a]
            bx, by, bh = pos[b]
            horiz = math.hypot(ax - bx, ay - by) / scale
            dz = (bh - ah) if (ah is not None and bh is not None) else 0.0
            slant = math.hypot(horiz, dz)
            s[f"{a}|{b}"] = [slant, slant, slant]
    return s


SQUARE = [("r1", 0, 0), ("r2", 100, 0), ("r3", 0, 100), ("r4", 100, 100)]


def test_selftest_recovers_receivers():
    res = bps.run_selftest(_hass_with(SQUARE, _exact_samples(SQUARE)))
    assert res["counts"]["solved"] == 4
    assert all(r["error_m"] < 0.5 for r in res["receivers"])


def test_selftest_recovers_with_mount_heights():
    recs = [("r1", 0, 0, 1.0), ("r2", 100, 0, 3.0), ("r3", 0, 100, 2.0), ("r4", 100, 100, 3.0)]
    res = bps.run_selftest(_hass_with(recs, _exact_samples(recs)))
    assert res["counts"]["solved"] == 4          # slant correction round-trips
    assert all(r["error_m"] < 0.5 for r in res["receivers"])


def test_selftest_unsolved_when_too_few_neighbors():
    recs = SQUARE + [("r5", 200, 200)]           # r5 has no samples at all
    res = bps.run_selftest(_hass_with(recs, _exact_samples(SQUARE)))
    solved = {r["entity"] for r in res["receivers"]}
    unsolved = {u["entity"] for u in res["unsolved"]}
    assert solved == {"r1", "r2", "r3", "r4"} and "r5" in unsolved


def test_selftest_error_grows_with_bad_distance():
    s = _exact_samples(SQUARE)
    for o in ("r2", "r3", "r4"):                  # inflate only r1's incoming links
        s[f"r1|{o}"] = [v * 1.6 for v in s[f"r1|{o}"]]
    res = bps.run_selftest(_hass_with(SQUARE, s))
    err = {r["entity"]: r["error_m"] for r in res["receivers"]}
    # Only the distorted receiver is dragged off; the others still solve clean.
    assert err["r1"] > 0.2 and max(err["r2"], err["r3"], err["r4"]) < 0.05


def test_selftest_empty_layout_is_safe():
    res = bps.run_selftest(make_hass())
    assert res["counts"]["placed"] == 0 and res["counts"]["solved"] == 0


def test_selftest_bounds_include_left_out_perimeter_receiver():
    # A receiver far outside the OTHER receivers' hull must still be recoverable:
    # the solver bounds cover ALL placed receivers (mirroring the live path), not
    # just the ones feeding this solve — otherwise it would clamp and over-report.
    recs = [("a", 0, 0), ("b", 50, 0), ("c", 0, 50), ("r", 200, 200)]
    res = bps.run_selftest(_hass_with(recs, _exact_samples(recs)))
    err = {x["entity"]: x["error_m"] for x in res["receivers"]}
    assert err["r"] < 0.5   # not clamped to the a/b/c bounding box


def test_selftest_summary_reports_cep_and_worst():
    result = {
        "counts": {"placed": 3, "solved": 2, "unsolved": 1},
        "receivers": [
            {"entity": "a", "floor": "F", "error_m": 1.0},
            {"entity": "b", "floor": "F", "error_m": 3.0},
        ],
        "unsolved": [{"entity": "c"}],
    }
    state, attrs = bps._selftest_summary(result)
    assert abs(state - attrs["cep95_m"]) < 1e-9        # state is CEP95
    assert abs(attrs["cep50_m"] - 2.0) < 1e-9          # median of [1, 3]
    assert abs(attrs["max_m"] - 3.0) < 1e-9 and abs(attrs["mean_m"] - 2.0) < 1e-9
    assert attrs["solved"] == 2 and attrs["placed"] == 3
    assert attrs["worst"].startswith("b")
    assert "F" in attrs["per_floor_cep95_m"]


def test_selftest_summary_unknown_when_none_solved():
    state, attrs = bps._selftest_summary(
        {"counts": {"placed": 4, "solved": 0, "unsolved": 4}, "receivers": [], "unsolved": []})
    assert state is None and "cep95_m" not in attrs and attrs["solved"] == 0


def test_selftest_summary_end_to_end_near_zero():
    res = bps.run_selftest(_hass_with(SQUARE, _exact_samples(SQUARE)))
    state, attrs = bps._selftest_summary(res)
    assert state is not None and state < 0.5 and attrs["solved"] == 4


def test_selftest_accepts_explicit_samples_snapshot():
    # The executor path passes a pre-snapshotted samples dict; run_selftest must
    # use it instead of reading the (possibly concurrently-mutated) live deques.
    hass = _hass_with(SQUARE, {})            # empty LIVE samples
    res = bps.run_selftest(hass, samples=_exact_samples(SQUARE))
    assert res["counts"]["solved"] == 4      # solved from the snapshot, not live state


def _linear_fit(points):
    """A plain (non-robust) weighted least-squares fit with trilaterate's exact
    objective, to prove soft_l1 does better on the SAME points."""
    import numpy as np
    from scipy.optimize import least_squares

    def obj(X):
        x, y = X
        res = [math.hypot(xi - x, yi - y) - ri for xi, yi, ri in points]
        w = [1.0 / max(ri, 1e-3) ** 2 for _xi, _yi, ri in points]
        return np.sqrt(np.array(w)) * np.array(res)

    c = [float(np.mean([p[0] for p in points])), float(np.mean([p[1] for p in points]))]
    return least_squares(obj, c, method="trf").x


def test_robust_loss_beats_linear_on_an_outlier():
    # Four good receivers pin (200,200); one comparably-weighted outlier at
    # (400,400) reports ~141 px when it is really ~283 px away (a ~2x short,
    # through-wall-style read). soft_l1 must pull the fit off it more than plain
    # linear least-squares does — measured against a linear fit on identical pts.
    truth = (200.0, 200.0)
    good = [(0.0, 200.0, 200.0), (400.0, 200.0, 200.0),
            (200.0, 0.0, 200.0), (200.0, 400.0, 200.0)]
    pts = good + [(400.0, 400.0, 141.0)]
    d_soft = math.dist(bps.trilaterate(pts), truth)
    d_linear = math.dist(_linear_fit(pts), truth)
    assert d_soft < d_linear              # robust loss helps...
    assert d_soft < 0.75 * d_linear       # ...by a clear margin (here ~0.62x)
