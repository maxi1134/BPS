"""Regression tests for the position history (the map's time scrubber).

Three things have to hold or the feature is worse than useless:

* the recording gate must keep a walk legible while a stationary device costs
  almost nothing,
* a query must span the WHOLE requested window after decimation (a trail that
  silently stops half-way is indistinguishable from the device stopping), and
* the on-disk record must survive a restart, a torn line, and a clear.
"""
import asyncio
import json
import os

import bps
from bps import history as H
from conftest import make_hass


ENT = "phone"
FLOOR = "Main"
SCALE = 40.0  # px per metre


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def hist(**over):
    cfg = H.history_config({})
    cfg.update(over)
    return H.PositionHistory(cfg)


def all_points(h, ent=ENT):
    return h.query(ent, 0, 1e12, 10 ** 9)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_defaults_are_the_documented_ones():
    cfg = H.history_config(None)
    assert cfg["max_age"] == H.DEFAULT_MAX_AGE
    assert cfg["min_interval"] == H.DEFAULT_MIN_INTERVAL
    assert cfg["min_move_m"] == H.DEFAULT_MIN_MOVE_M
    assert cfg["heartbeat"] == H.DEFAULT_HEARTBEAT
    assert cfg["enabled"] is True


def test_max_age_is_clamped_to_the_supported_ceiling():
    # An unbounded window would grow the ring until the process dies.
    assert H.history_config({"history_max_age": 99_999_999})["max_age"] == H.MAX_AGE_LIMIT
    assert H.history_config({"history_max_age": -5})["max_age"] > 0


def test_booleans_are_not_accepted_as_numbers():
    # isinstance(True, int) holds in Python: a hand-edited `true` must fall
    # back to the default, not silently configure a 1-metre move threshold.
    assert H.history_config({"history_min_move_m": True})["min_move_m"] == H.DEFAULT_MIN_MOVE_M


def test_disabled_records_nothing():
    h = hist(enabled=False)
    assert h.record(ENT, 1000.0, 0.0, 0.0, FLOOR, SCALE) is False
    assert h.entities() == []


# --------------------------------------------------------------------------- #
# The recording gate
# --------------------------------------------------------------------------- #
def test_a_stationary_device_only_costs_its_heartbeat():
    h = hist()
    t0 = 1_000_000.0
    for i in range(120):           # 120 s of standing still, 1 Hz
        h.record(ENT, t0 + i, 5.0, 5.0, FLOOR, SCALE)
    # First point + one per 30 s heartbeat.
    assert all_points(h)["count"] == 4


def test_a_walk_is_kept_at_the_minimum_interval():
    h = hist()
    t0 = 1_000_000.0
    for i in range(60):            # 1 m/s for a minute
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    assert all_points(h)["count"] == 30   # every 2 s


def test_jitter_below_the_move_threshold_is_dropped():
    h = hist()
    t0 = 1_000_000.0
    for i in range(30):            # 10 cm of noise every 2 s for a minute
        h.record(ENT, t0 + i * 2, 0.1 * (i % 2), 0.0, FLOOR, SCALE)
    # Only the first point and the 30 s heartbeat: none of the jitter earns a
    # point of its own, so a device sitting on a table costs almost nothing.
    assert all_points(h)["count"] == 2


def test_a_floor_change_is_always_kept_and_breaks_the_line():
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 1.0, 1.0, "A", 40.0)
    # Sub-interval and sub-movement, but a different floor: the two points live
    # in different pixel frames, so joining them would draw a nonsense segment.
    assert h.record(ENT, t0 + 0.5, 1.0, 1.0, "B", 50.0) is True
    got = all_points(h)
    assert got["count"] == 2
    assert got["gap"] == [1, 1]
    assert got["floors"] == ["A", "B"]
    assert got["scales"] == [40.0, 50.0]


def test_clock_going_backwards_restarts_the_buffer():
    # The arrays are searched with bisect everywhere, so one out-of-order
    # append would make query() and evict() return nonsense. A clock that steps
    # backwards therefore starts the buffer over rather than corrupting it.
    h = hist()
    for i in range(5):
        h.record(ENT, 1_000_000.0 + i * 5, float(i), 0.0, FLOOR, SCALE)
    h.record(ENT, 999_000.0, 9.0, 9.0, FLOOR, SCALE)
    got = all_points(h)
    assert got["t"] == [999_000.0]
    assert got["gap"] == [1]
    assert list(got["t"]) == sorted(got["t"])


def test_a_dropout_breaks_the_line_before_the_prune_timeout():
    # A device unheard for a couple of minutes was not standing still; joining
    # across the silence would draw a straight line through whatever it
    # actually did. The explicit mark_gap only arrives after the much longer
    # position_timeout, so the recorder has to notice this itself.
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 0.0, 0.0, FLOOR, SCALE)
    h.record(ENT, t0 + 20, 5.0, 0.0, FLOOR, SCALE)     # normal, no break
    h.record(ENT, t0 + 200, 9.0, 0.0, FLOOR, SCALE)    # after a silence
    assert all_points(h)["gap"] == [1, 0, H.GAP_DROPOUT]


def test_mark_gap_breaks_the_next_segment():
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 0.0, 0.0, FLOOR, SCALE)
    h.record(ENT, t0 + 5, 5.0, 0.0, FLOOR, SCALE)
    h.mark_gap(ENT)                      # tracker pruned for absence
    h.record(ENT, t0 + 600, 40.0, 0.0, FLOOR, SCALE)
    # 2 = the device really was unheard across it, not merely a new polyline.
    assert all_points(h)["gap"] == [1, 0, H.GAP_DROPOUT]


def test_non_finite_and_unnamed_input_is_refused():
    h = hist()
    assert h.record(ENT, float("nan"), 0.0, 0.0, FLOOR, SCALE) is False
    assert h.record(ENT, 1000.0, float("inf"), 0.0, FLOOR, SCALE) is False
    assert h.record("", 1000.0, 0.0, 0.0, FLOOR, SCALE) is False
    assert h.entities() == []


# --------------------------------------------------------------------------- #
# Eviction
# --------------------------------------------------------------------------- #
def test_points_older_than_the_window_are_evicted():
    # Regression: compaction used to be gated on `max_points // 10`, so a ring
    # holding far fewer points than the cap (6 h at one point per 2 s is ~10k
    # against a 200k cap) never compacted at all and max_age was ignored.
    h = hist(max_age=100.0)
    t0 = 1_000_000.0
    for i in range(0, 4000, 4):        # 1000 fixes; ~25 fit the window
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    got = all_points(h)
    assert got["t"][-1] == t0 + 3996   # the newest fix is always kept
    # Compaction is amortised, so a few expired points may linger; what must
    # hold is that the buffer tracks the WINDOW and not the length of the run.
    assert got["count"] < 40
    assert got["t"][-1] - got["t"][0] <= 100.0 * 1.5


def test_max_points_bounds_memory_independently_of_age():
    h = hist(max_age=10 ** 9, max_points=50)
    t0 = 1_000_000.0
    for i in range(500):
        h.record(ENT, t0 + i * 4, float(i), 0.0, FLOOR, SCALE)
    assert all_points(h)["count"] <= 50


def test_the_surviving_head_starts_a_new_line_after_eviction():
    # Whatever preceded the retained head is gone, so the first kept point must
    # not be joined to a predecessor that no longer exists.
    h = hist(max_age=50.0)
    t0 = 1_000_000.0
    for i in range(0, 200, 2):
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    assert all_points(h)["gap"][0] == 1


# --------------------------------------------------------------------------- #
# Query + decimation
# --------------------------------------------------------------------------- #
def test_query_windows_to_the_requested_range():
    h = hist(max_age=10 ** 9)
    t0 = 1_000_000.0
    for i in range(0, 600, 3):
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    got = h.query(ENT, t0 + 100, t0 + 200, 10 ** 9)
    assert got["count"] > 0
    assert all(t0 + 100 <= t <= t0 + 200 for t in got["t"])


def test_decimation_still_spans_the_whole_window():
    # The failure this guards against: a naive "first N points" cap makes the
    # trail stop early, which reads as the device having stopped moving.
    h = hist(max_age=10 ** 9, max_points=10 ** 9)
    t0 = 1_000_000.0
    for i in range(1000):
        h.record(ENT, t0 + i * 3, float(i), 0.0, FLOOR, SCALE)
    got = h.query(ENT, t0, t0 + 10 ** 6, 50)
    assert got["count"] <= 60
    assert got["t"][0] == t0
    assert got["t"][-1] == t0 + 999 * 3
    assert got["stride"] > 1


def test_decimation_never_drops_a_gap_or_a_floor_change():
    h = hist(max_age=10 ** 9, max_points=10 ** 9)
    t0 = 1_000_000.0
    for i in range(300):
        floor = "A" if i < 150 else "B"
        h.record(ENT, t0 + i * 3, float(i), 0.0, floor, SCALE)
    got = h.query(ENT, t0, t0 + 10 ** 6, 20)
    # The floor change is a gap, and every gap must survive the stride.
    assert sum(got["gap"]) >= 2
    assert set(got["floors"][i] for i in got["f"]) == {"A", "B"}


def test_query_of_an_unknown_tracker_is_empty_not_an_error():
    got = hist().query("nobody", 0, 1e12, 100)
    assert got["count"] == 0 and got["t"] == []


def test_retained_reports_the_actual_span():
    h = hist(max_age=10 ** 9)
    t0 = 1_000_000.0
    for i in range(0, 100, 5):
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    span = h.retained(ENT)
    assert span["from"] == t0 and span["to"] == t0 + 95 and span["points"] == 20
    assert h.retained("nobody") is None


# --------------------------------------------------------------------------- #
# Disk segments
# --------------------------------------------------------------------------- #
def flush_to(h, dirpath):
    return H.append_segments(dirpath, h.drain_pending())


def test_round_trip_through_disk_is_lossless(tmp_path):
    d = str(tmp_path / "hist")
    h = hist(max_age=10 ** 9)
    import time as _t
    t0 = _t.time() - 500
    for i in range(100):
        h.record(ENT, t0 + i * 5, float(i) * 0.5, float(i) * 0.25, FLOOR, SCALE)
    assert flush_to(h, d) == all_points(h)["count"]

    before = all_points(h)
    back = hist(max_age=10 ** 9)
    back.load_rows(H.restore_recent(d, back.cfg))
    after = all_points(back)
    assert after["count"] == before["count"]
    assert max(abs(a - b) for a, b in zip(after["t"], before["t"])) == 0
    assert max(abs(a - b) for a, b in zip(after["x_m"], before["x_m"])) < 1e-3
    assert after["floors"] == before["floors"]


def test_a_torn_final_line_costs_only_that_line(tmp_path):
    d = str(tmp_path / "hist")
    h = hist(max_age=10 ** 9)
    import time as _t
    t0 = _t.time() - 200
    for i in range(20):
        h.record(ENT, t0 + i * 5, float(i), 0.0, FLOOR, SCALE)
    flush_to(h, d)
    day = H.day_key(t0)
    path = H.segment_path(d, day)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"e":"phone","t":123')      # power cut mid-append
    back = hist(max_age=10 ** 9)
    back.load_rows(H.restore_recent(d, back.cfg))
    assert all_points(back)["count"] == 20


def test_restore_ignores_rows_outside_the_window(tmp_path):
    d = str(tmp_path / "hist")
    os.makedirs(d, exist_ok=True)
    import time as _t
    now = _t.time()
    lines = [json.dumps({"e": ENT, "t": now - 100, "x": 1.0, "y": 1.0, "f": FLOOR, "s": SCALE}),
             json.dumps({"e": ENT, "t": now - 10 ** 6, "x": 9.0, "y": 9.0, "f": FLOOR, "s": SCALE})]
    H.append_segments(d, {H.day_key(now): lines})
    back = hist()
    back.load_rows(H.read_segments(d, H.list_day_keys(d)))
    assert all_points(back)["count"] == 1


def test_expired_day_segments_are_pruned(tmp_path):
    d = str(tmp_path / "hist")
    import time as _t
    now = _t.time()
    H.append_segments(d, {
        "20200101": [json.dumps({"e": ENT, "t": 1.0, "x": 0, "y": 0, "f": FLOOR})],
        H.day_key(now): [json.dumps({"e": ENT, "t": now, "x": 0, "y": 0, "f": FLOOR})],
    })
    removed = H.prune_segments(d, 6 * 3600, now)
    assert removed == ["20200101"]
    assert H.list_day_keys(d) == [H.day_key(now)]


def test_drop_entity_rewrites_segments_without_that_tracker(tmp_path):
    d = str(tmp_path / "hist")
    import time as _t
    now = _t.time()
    day = H.day_key(now)
    H.append_segments(d, {day: [
        json.dumps({"e": "a", "t": now - 3, "x": 1, "y": 1, "f": FLOOR}),
        json.dumps({"e": "b", "t": now - 2, "x": 2, "y": 2, "f": FLOOR}),
        json.dumps({"e": "a", "t": now - 1, "x": 3, "y": 3, "f": FLOOR}),
    ]})
    assert H.drop_entity(d, "a", H.history_config({})) == 2
    rows = H.read_segments(d, H.list_day_keys(d))
    assert [r["e"] for r in rows] == ["b"]


def test_drop_entity_removes_a_segment_it_empties(tmp_path):
    d = str(tmp_path / "hist")
    import time as _t
    now = _t.time()
    H.append_segments(d, {H.day_key(now): [
        json.dumps({"e": "a", "t": now, "x": 1, "y": 1, "f": FLOOR})]})
    H.drop_entity(d, "a", H.history_config({}))
    assert H.list_day_keys(d) == []
    assert not os.path.exists(H.segment_path(d, H.day_key(now)) + ".tmp")


def test_forget_drops_only_that_trackers_queued_rows():
    # A clear must not be undone by a flush of rows queued moments earlier —
    # and must not take the other trackers' rows down with it.
    h = hist()
    t0 = 1_000_000.0
    h.record("a", t0, 0.0, 0.0, FLOOR, SCALE)
    h.record("b", t0, 0.0, 0.0, FLOOR, SCALE)
    h.forget("a")
    grouped = h.drain_pending()
    rows = [json.loads(line) for lines in grouped.values() for line in lines]
    assert [r["e"] for r in rows] == ["b"]
    assert h.entities() == ["b"]


def test_pending_queue_is_bounded():
    h = hist(max_age=10 ** 9, max_points=10 ** 9)
    h.MAX_PENDING = 10
    t0 = 1_000_000.0
    for i in range(100):
        h.record(ENT, t0 + i * 5, float(i), 0.0, FLOOR, SCALE)
    assert h.pending_count() == 10
    assert h.dropped_pending > 0


# --------------------------------------------------------------------------- #
# The HTTP view + integration wiring
# --------------------------------------------------------------------------- #
class _Req:
    def __init__(self, query=None, body=None):
        self.query = query or {}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def api(hass):
    return bps.BPSHistoryAPI(hass)


def seeded_hass(tmp_path, **layout):
    hass = make_hass(tmp_path)
    hass.data["bps"] = {"layout": dict(layout)}
    h = bps.get_position_history(hass)
    import time as _t
    t0 = _t.time() - 300
    for i in range(60):
        h.record(ENT, t0 + i * 5, float(i) * 0.5, 0.0, FLOOR, SCALE)
    return hass, h, t0


def test_index_lists_what_is_retained(tmp_path):
    hass, h, _ = seeded_hass(tmp_path)
    res = run(api(hass).get(_Req()))
    body = res.json_body
    assert [t["ent"] for t in body["trackers"]] == [ENT]
    assert body["trackers"][0]["points"] == h.retained(ENT)["points"]
    assert body["config"]["max_age"] == H.DEFAULT_MAX_AGE


def test_query_returns_metres_and_the_recording_scale(tmp_path):
    hass, _, t0 = seeded_hass(tmp_path)
    res = run(api(hass).get(_Req({"entity": ENT, "from": str(t0), "to": str(t0 + 10 ** 6)})))
    body = res.json_body
    assert body["count"] > 0
    assert body["floors"] == [FLOOR] and body["scales"] == [SCALE]
    assert body["x_m"][0] == 0.0            # metres, not pixels
    assert body["retained"]["points"] >= body["count"]


def test_query_honours_max_points(tmp_path):
    hass, _, t0 = seeded_hass(tmp_path)
    res = run(api(hass).get(_Req({"entity": ENT, "from": str(t0),
                                  "to": str(t0 + 10 ** 6), "max_points": "5"})))
    assert res.json_body["count"] <= 6


def test_junk_query_params_fall_back_instead_of_500ing(tmp_path):
    hass, _, _ = seeded_hass(tmp_path)
    res = run(api(hass).get(_Req({"entity": ENT, "from": "yesterday",
                                  "to": "NaN", "max_points": "1e999"})))
    assert res.status == 200 and res.json_body["count"] > 0


def test_reversed_window_is_normalised(tmp_path):
    hass, _, t0 = seeded_hass(tmp_path)
    res = run(api(hass).get(_Req({"entity": ENT, "from": str(t0 + 10 ** 6), "to": str(t0)})))
    assert res.json_body["count"] > 0


def test_layout_settings_reach_the_recorder(tmp_path):
    hass, h, _ = seeded_hass(tmp_path)
    hass.data["bps"]["layout"]["history_max_age"] = 900
    run(api(hass).get(_Req()))
    assert h.cfg["max_age"] == 900.0


def test_post_requires_a_known_action(tmp_path):
    hass, _, _ = seeded_hass(tmp_path)
    assert run(api(hass).post(_Req(body={"action": "nope"}))).status == 400
    assert run(api(hass).post(_Req(body=["not", "an", "object"]))).status == 400
    assert run(api(hass).post(_Req())).status == 400


def test_clear_forgets_memory_and_disk(tmp_path):
    hass, h, _ = seeded_hass(tmp_path)
    run(bps.flush_position_history(hass))
    assert H.list_day_keys(bps.history_dir(hass))
    res = run(api(hass).post(_Req(body={"action": "clear"})))
    assert res.status == 200
    assert h.entities() == []
    assert H.list_day_keys(bps.history_dir(hass)) == []


def test_clear_of_one_tracker_leaves_the_others(tmp_path):
    hass, h, _ = seeded_hass(tmp_path)
    import time as _t
    h.record("other", _t.time(), 1.0, 1.0, FLOOR, SCALE)
    run(bps.flush_position_history(hass))
    run(api(hass).post(_Req(body={"action": "clear", "entity": ENT})))
    assert h.entities() == ["other"]
    rows = H.read_segments(bps.history_dir(hass), H.list_day_keys(bps.history_dir(hass)))
    assert {r["e"] for r in rows} == {"other"}


def test_restore_after_a_restart_reloads_and_breaks_the_line(tmp_path):
    hass, h, _ = seeded_hass(tmp_path)
    kept = h.retained(ENT)["points"]
    run(bps.flush_position_history(hass))

    # "Restart": same config dir, fresh in-memory history.
    fresh = make_hass(tmp_path)
    fresh.data["bps"] = {"layout": {}}
    run(bps.restore_position_history(fresh))
    back = bps.get_position_history(fresh)
    assert back.retained(ENT)["points"] == kept
    # Nothing must be written back out: the rows are already on disk.
    assert back.pending_count() == 0
    # The next fix after an outage of unknown length starts a new polyline.
    import time as _t
    back.record(ENT, _t.time(), 99.0, 99.0, FLOOR, SCALE)
    assert all_points(back, ENT)["gap"][-1] == H.GAP_DROPOUT


def test_history_is_stored_outside_the_web_root(tmp_path):
    # www/ is served to anyone who can guess a URL; the movement record is not
    # something to publish. It lives under .storage with the rest of the state.
    hass = make_hass(tmp_path)
    path = bps.history_dir(hass).replace("\\", "/")
    assert "/.storage/" in path and "/www/" not in path


# --------------------------------------------------------------------------- #
# Regressions from the adversarial review of this feature
# --------------------------------------------------------------------------- #
def test_a_reload_does_not_duplicate_the_ring(tmp_path):
    # async_unload_entry cancels the tracking task but leaves hass.data alone,
    # so setup re-enters with the SAME PositionHistory. Re-reading the segments
    # there appended a second copy of every point and left the arrays unsorted,
    # which breaks every bisect in query() and evict().
    hass, h, _ = seeded_hass(tmp_path)
    run(bps.flush_position_history(hass))
    import time as _t
    h.record(ENT, _t.time(), 42.0, 42.0, FLOOR, SCALE)   # not yet flushed
    before = h.retained(ENT)["points"]
    pending = h.pending_count()

    run(bps.restore_position_history(hass))              # the reload

    assert h.retained(ENT)["points"] == before
    assert list(h.tracks[ENT].t) == sorted(h.tracks[ENT].t)
    assert h.pending_count() == pending                  # unflushed rows kept


def test_clear_is_not_undone_by_a_concurrent_flush(tmp_path):
    # The flush drains its rows and writes them in the executor; a clear that
    # landed in between used to be overwritten by that write, so the forgotten
    # positions came back on the next restart.
    hass, h, _ = seeded_hass(tmp_path)

    async def both():
        await asyncio.gather(
            bps.flush_position_history(hass),
            api(hass).post(_Req(body={"action": "clear"})),
        )

    asyncio.new_event_loop().run_until_complete(both())
    d = bps.history_dir(hass)
    assert H.read_segments(d, H.list_day_keys(d)) == []
    assert h.entities() == []


def test_pruning_is_skipped_when_the_retention_cannot_be_trusted(tmp_path):
    # Pruning deletes days irreversibly. With no layout dict (fresh install, or
    # a store that failed to load this boot) history_config falls back to the
    # 6 h default, which would take a configured 7-day record down to six hours.
    import time as _t
    hass = make_hass(tmp_path)
    hass.data["bps"] = {"layout": []}          # not a dict
    d = bps.history_dir(hass)
    old_day = H.day_key(_t.time() - 3 * 86400)
    H.append_segments(d, {old_day: [json.dumps(
        {"e": ENT, "t": 1.0, "x": 0, "y": 0, "f": FLOOR})]})
    run(bps.flush_position_history(hass, prune=True))
    assert H.list_day_keys(d) == [old_day]     # still there

    hass.data["bps"]["layout"] = {"history_max_age": 3600}
    run(bps.flush_position_history(hass, prune=True))
    assert H.list_day_keys(d) == []            # now it is safe to prune


def test_a_silent_tracker_stops_being_served_past_the_window():
    # record() only ages the track it touched, so a device that left the house
    # kept serving its last position long past the configured retention.
    h = hist(max_age=100.0)
    t0 = 1_000_000.0
    for i in range(0, 60, 5):
        h.record(ENT, t0 + i, float(i), 0.0, FLOOR, SCALE)
    assert h.retained(ENT)["points"] > 0
    h.evict_all(now=t0 + 10_000)
    assert h.retained(ENT) is None


def test_query_is_capped_even_when_every_point_is_a_break():
    # Gaps and floor changes are force-kept, so data that flaps between floors
    # defeats the stride entirely - megabytes of JSON for a small request.
    h = hist(max_age=10 ** 9, max_points=10 ** 9)
    t0 = 1_000_000.0
    for i in range(4000):
        h.record(ENT, t0 + i * 3, float(i), 0.0, "A" if i % 2 else "B", SCALE)
    got = h.query(ENT, t0, t0 + 10 ** 6, 50)
    assert got["count"] <= 101          # the hard cap is 2x the request
    assert got["t"][0] == t0 and got["t"][-1] == t0 + 3999 * 3


def test_a_torn_tail_does_not_swallow_the_next_good_row(tmp_path):
    import time as _t
    d = str(tmp_path / "hist")
    now = _t.time()
    day = H.day_key(now)
    H.append_segments(d, {day: [json.dumps({"e": ENT, "t": now - 9, "x": 1, "y": 1, "f": FLOOR})]})
    with open(H.segment_path(d, day), "a", encoding="utf-8") as fh:
        fh.write('{"e":"phone","t":123')            # died mid-append, no newline
    H.append_segments(d, {day: [json.dumps({"e": ENT, "t": now - 1, "x": 2, "y": 2, "f": FLOOR})]})
    rows = H.read_segments(d, [day])
    # The torn line is lost (unavoidable); the row appended after it is not.
    assert [r["x"] for r in rows] == [1, 2]


def test_one_corrupt_byte_costs_one_line_not_the_restore(tmp_path):
    # UnicodeDecodeError is not an OSError, so it escaped the guard and took
    # the whole restore (and per-tracker clear) down with it.
    import time as _t
    d = str(tmp_path / "hist")
    now = _t.time()
    day = H.day_key(now)
    H.append_segments(d, {day: [
        json.dumps({"e": ENT, "t": now - 9, "x": 1, "y": 1, "f": FLOOR}),
        json.dumps({"e": ENT, "t": now - 8, "x": 2, "y": 2, "f": FLOOR}),
    ]})
    with open(H.segment_path(d, day), "ab") as fh:
        fh.write(b"\xff\xfe not utf-8 at all\n")
    rows = H.read_segments(d, [day])
    assert [r["x"] for r in rows] == [1, 2]
    assert H.drop_entity(d, ENT, H.history_config({})) == 2


def test_orphaned_tmp_files_are_swept(tmp_path):
    # list_day_keys ignores .tmp, so a rewrite that died left a full copy of
    # the record that neither Clear nor the pruner ever removed.
    import time as _t
    d = str(tmp_path / "hist")
    now = _t.time()
    H.append_segments(d, {H.day_key(now): [json.dumps(
        {"e": ENT, "t": now, "x": 1, "y": 1, "f": FLOOR})]})
    orphan = H.segment_path(d, "20200101") + ".tmp"
    with open(orphan, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"e": ENT, "t": 1.0, "x": 9, "y": 9, "f": FLOOR}) + "\n")
    H.prune_segments(d, 3600, now)
    assert not os.path.exists(orphan)
    H.clear_segments(d)
    assert os.listdir(d) == []


def test_a_failed_write_puts_the_rows_back():
    h = hist()
    t0 = 1_000_000.0
    for i in range(10):
        h.record(ENT, t0 + i * 40, float(i), 0.0, FLOOR, SCALE)
    grouped = h.drain_pending()
    assert h.pending_count() == 0
    h.requeue(grouped)
    assert h.pending_count() == 10
    rows = [json.loads(line) for lines in h.drain_pending().values() for line in lines]
    assert [r["e"] for r in rows] == [ENT] * 10


def test_coordinates_are_coerced_not_type_checked():
    # The solver hands back whatever numpy type the Kalman/clip path produced.
    # np.float32 is NOT a subclass of float, so an isinstance gate would have
    # silently recorded nothing at all.
    from decimal import Decimal

    class Float32Like(float):
        pass

    h = hist()
    assert h.record(ENT, 1_000_000.0, Float32Like(1.5), Decimal("2.5"), FLOOR, SCALE)
    got = all_points(h)
    assert got["x_m"] == [1.5] and got["y_m"] == [2.5]


def test_a_giant_json_number_in_a_segment_is_skipped_not_fatal(tmp_path):
    # math.isfinite() raises OverflowError on an int with hundreds of digits,
    # which would abort the restore rather than skip the bad row.
    import time as _t
    d = str(tmp_path / "hist")
    now = _t.time()
    day = H.day_key(now)
    H.append_segments(d, {day: [
        '{"e":"phone","t":' + "9" * 400 + ',"x":1,"y":1,"f":"Main"}',
        json.dumps({"e": ENT, "t": now - 1, "x": 3, "y": 3, "f": FLOOR}),
    ]})
    back = hist()
    assert back.load_rows(H.read_segments(d, [day])) == 1
    assert all_points(back)["x_m"] == [3.0]


def test_prune_keeps_exactly_what_restore_reads_back(tmp_path):
    # The pruner used a day of extra slack the restore then filtered out, so
    # the oldest kept segment was never read.
    import time as _t
    d = str(tmp_path / "hist")
    now = _t.time()
    cfg = H.history_config({"history_max_age": 6 * 3600})
    for back_days in range(4):
        day = H.day_key(now - back_days * 86400)
        H.append_segments(d, {day: [json.dumps(
            {"e": ENT, "t": now - back_days * 86400, "x": back_days, "y": 0, "f": FLOOR})]})
    H.prune_segments(d, cfg["max_age"], now)
    kept = set(H.list_day_keys(d))
    read = {H.day_key(r["t"]) for r in H.restore_recent(d, cfg, now)}
    assert kept == read


# --------------------------------------------------------------------------- #
# The recorded room (for the map's room band)
# --------------------------------------------------------------------------- #
def test_the_room_is_recorded_and_returned_per_point():
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 0.0, 0.0, FLOOR, SCALE, "Kitchen")
    h.record(ENT, t0 + 40, 9.0, 0.0, FLOOR, SCALE, "Hall")
    got = all_points(h)
    assert [got["zones"][i] for i in got["z"]] == ["Kitchen", "Hall"]


def test_index_zero_always_means_unknown():
    # An overflow or a missing zone must read as "no idea", never as whichever
    # room happened to be interned first.
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 0.0, 0.0, FLOOR, SCALE, "Kitchen")
    h.record(ENT, t0 + 40, 9.0, 0.0, FLOOR, SCALE, None)
    h.record(ENT, t0 + 80, 18.0, 0.0, FLOOR, SCALE, "unknown")
    got = all_points(h)
    assert got["zones"][0] == ""
    assert [got["zones"][i] for i in got["z"]] == ["Kitchen", "", ""]


def test_a_room_change_is_kept_as_soon_as_the_interval_allows():
    # A room change counts alongside movement, so the band's edge lands within
    # min_interval instead of up to a heartbeat late...
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 1.0, 1.0, FLOOR, SCALE, "Kitchen")
    assert h.record(ENT, t0 + 2.0, 1.05, 1.0, FLOOR, SCALE, "Hall") is True
    got = all_points(h)
    assert [got["zones"][i] for i in got["z"]] == ["Kitchen", "Hall"]
    # ...and it is the same continuous walk, so it must NOT break the line.
    assert got["gap"] == [1, 0]


def test_a_flickering_room_boundary_cannot_defeat_the_interval():
    # Zone assignment is pure geometry with no hysteresis, so a device parked on
    # a boundary flips room on BLE noise alone. Exempting a room change from
    # min_interval recorded every single fix of that (measured 299 rows instead
    # of 20 for a device that moved 2 cm).
    h = hist()
    t0 = 1_000_000.0
    for i in range(600):                      # 10 min at 1 Hz, standing still
        h.record(ENT, t0 + i, 5.0, 5.0, FLOOR, SCALE,
                 "Kitchen" if i % 2 else "Hall")
    kept = all_points(h)["count"]
    assert kept <= 600 / 2 + 2                # bounded by min_interval, not 1 Hz


def test_a_silence_still_breaks_the_line_when_the_room_changed():
    # The room-change keep used to bypass the dropout check, so an outage that
    # ended in a different room was painted as a solid run of the OLD room and
    # the trail drew straight through it.
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 1.0, 1.0, FLOOR, SCALE, "Kitchen")
    h.record(ENT, t0 + 200, 9.0, 1.0, FLOOR, SCALE, "Hall")
    assert all_points(h)["gap"] == [1, H.GAP_DROPOUT]


def test_a_floor_change_is_a_frame_break_not_a_dropout():
    # Both break the drawn line, but only one means "nothing was recorded":
    # the room band must not paint a hole over data it has.
    h = hist()
    t0 = 1_000_000.0
    h.record(ENT, t0, 1.0, 1.0, "A", 40.0, "Kitchen")
    h.record(ENT, t0 + 3, 1.0, 1.0, "B", 50.0, "Landing")
    assert all_points(h)["gap"] == [H.GAP_FRAME, H.GAP_FRAME]


def test_the_255th_zone_reads_as_unknown_not_as_the_first_room():
    h = hist(max_age=10 ** 9, max_points=10 ** 9)
    t0 = 1_000_000.0
    for i in range(300):
        h.record(ENT, t0 + i * 40, float(i), 0.0, FLOOR, SCALE, "room%d" % i)
    got = all_points(h)
    names = [got["zones"][i] for i in got["z"]]
    assert names[0] == "room0"
    assert names[-1] == ""            # past the byte column: unknown, not room0
    assert len(got["zones"]) == 255


def test_rooms_survive_the_disk_round_trip(tmp_path):
    import time as _t
    d = str(tmp_path / "hist")
    h = hist(max_age=10 ** 9)
    t0 = _t.time() - 400
    for i in range(20):
        h.record(ENT, t0 + i * 20, float(i), 0.0, FLOOR, SCALE,
                 "Kitchen" if i < 10 else "Hall")
    H.append_segments(d, h.drain_pending())
    back = hist(max_age=10 ** 9)
    back.load_rows(H.restore_recent(d, back.cfg))
    got = all_points(back)
    names = [got["zones"][i] for i in got["z"]]
    assert names[:10] == ["Kitchen"] * 10 and names[10:] == ["Hall"] * 10


def test_decimation_keeps_every_room_boundary():
    # The band is drawn from these transitions; a dropped one merges two rooms.
    h = hist(max_age=10 ** 9, max_points=10 ** 9)
    t0 = 1_000_000.0
    for i in range(600):
        h.record(ENT, t0 + i * 3, float(i), 0.0, FLOOR, SCALE,
                 "Kitchen" if (i // 50) % 2 == 0 else "Hall")
    got = h.query(ENT, t0, t0 + 10 ** 6, 20)
    names = [got["zones"][i] for i in got["z"]]
    changes = sum(1 for i in range(1, len(names)) if names[i] != names[i - 1])
    assert changes == 11          # 600/50 - 1 boundaries, all preserved


def test_a_query_with_no_points_still_carries_the_zone_table():
    got = hist().query("nobody", 0, 1e12, 100)
    assert got["zones"] == [""] and got["z"] == []


def test_history_without_a_recorded_room_still_works():
    # Points written before rooms were recorded restore with no zone at all.
    h = hist()
    t0 = 1_000_000.0
    h.load_rows([{"e": ENT, "t": t0, "x": 1.0, "y": 1.0, "f": FLOOR, "s": SCALE}],
                now=t0)
    got = all_points(h)
    assert got["count"] == 1
    assert [got["zones"][i] for i in got["z"]] == [""]
