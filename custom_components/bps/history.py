"""Bounded position history for tracked devices (the panel time scrubber).

Why BPS keeps this itself rather than leaning on Home Assistant's recorder:
the headline requirement is a CONFIGURABLE maximum span, and `purge_keep_days`
is global, day-granularity and at least one day - you cannot keep six hours of
BPS and ten days of everything else. Writing a 1 Hz position stream into the
recorder would also add tens of megabytes a day to every install on upgrade,
and BPS publishes no position entity today, so nothing is recorded anyway.

Shape of the data: points are stored in METRES within their floor's frame plus
the floor NAME, never in map pixels. Re-exporting a map image at a different
resolution changes every pixel coordinate but not where the device physically
was, so the pixel projection is done at render time from the floor's current
scale. The scale in effect when each floor row was first written is kept
alongside it, so a later scale change can be detected.

Storage is columnar (`array`), which is the difference between ~17 bytes and
~264 bytes per point - i.e. between ~2 MB and ~34 MB resident for 24 h across
three trackers. Durability is append-only NDJSON day files, so a crash can
only cost the last un-flushed batch, never the whole history the way rewriting
a single blob could (the lesson of issue #104).

This module imports nothing from the rest of the package, so any writer can use
it without an import cycle, and it performs no I/O itself - the caller takes
the lines to append and hands back rows to restore, doing the file access in an
executor.
"""
from array import array
import bisect
import json
import math
import os
import time

DOMAIN = "bps"

# Sub-directory under config/.storage. NOT under www/: history must never be
# web-served, the same reasoning that moved the layout out of www in #104.
HISTORY_DIRNAME = "bps_history"

# Defaults, all overridable per install from top-level layout keys.
DEFAULT_MAX_AGE = 6 * 3600           # keep 6 h unless asked for more
MAX_AGE_LIMIT = 7 * 24 * 3600        # 7 days, the supported ceiling
DEFAULT_MAX_POINTS = 200_000         # per tracker; OOM guard, independent of age
DEFAULT_MIN_INTERVAL = 2.0           # s between kept points
DEFAULT_MIN_MOVE_M = 0.3             # m of movement to be worth keeping
DEFAULT_HEARTBEAT = 30.0             # s: keep a point even when standing still

_CFG_SPEC = {
    # layout key            default              lo       hi
    "history_max_age":      (DEFAULT_MAX_AGE,    60.0,    float(MAX_AGE_LIMIT)),
    "history_max_points":   (DEFAULT_MAX_POINTS, 500.0,   500_000.0),
    "history_min_interval": (DEFAULT_MIN_INTERVAL, 0.5,   60.0),
    "history_min_move_m":   (DEFAULT_MIN_MOVE_M,  0.05,   10.0),
    "history_heartbeat":    (DEFAULT_HEARTBEAT,   5.0,    600.0),
}


# A dropout longer than this (and longer than the heartbeat allows for) is
# treated as a break in the record rather than a long straight line: the device
# was not simply standing still, it was not being heard at all.
DROPOUT_GAP_FACTOR = 3.0
DROPOUT_GAP_MIN = 90.0


def _finite(value):
    """`value` as a finite float, or None.

    Coerces rather than type-checks: the solver hands back whatever numpy type
    the Kalman/clip path produced (np.float32 is NOT a subclass of float, so an
    isinstance gate would silently record nothing), and a restored row comes
    from JSON. Rejects bools, and survives a JSON integer with hundreds of
    digits - float() raises OverflowError there, where math.isfinite() would
    raise it too and take the whole restore down with it.
    """
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def history_config(layout):
    """Resolve the history settings from the layout, clamped to sane ranges."""
    cfg = {}
    src = layout if isinstance(layout, dict) else {}
    for key, (default, lo, hi) in _CFG_SPEC.items():
        value = src.get(key)
        # not-bool: isinstance(True, int) holds in Python, so a hand-edited
        # true/false would otherwise read as a valid 1/0.
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(value):
            value = min(max(float(value), lo), hi)
        else:
            value = float(default)
        cfg[key[len("history_"):]] = value
    enabled = src.get("history_enabled")
    cfg["enabled"] = True if enabled is None else bool(enabled)
    cfg["max_points"] = int(cfg["max_points"])
    return cfg


class _Track:
    """One tracker's columnar ring buffer."""

    __slots__ = ("t", "x", "y", "f", "gap", "floors", "scales",
                 "last_kept", "force_gap")

    def __init__(self):
        self.t = array("d")
        self.x = array("f")
        self.y = array("f")
        self.f = array("B")       # index into self.floors
        self.gap = array("B")     # 1 = this point STARTS a new polyline
        self.floors = []          # append-only: index -> floor name
        self.scales = []          # px/m in effect when that floor was first seen
        self.last_kept = None     # (t, x, y, floor_index)
        self.force_gap = False

    def floor_index(self, floor, scale):
        name = "" if floor is None else str(floor)
        try:
            return self.floors.index(name)
        except ValueError:
            if len(self.floors) >= 255:   # f is a byte column
                return 0
            self.floors.append(name)
            self.scales.append(float(scale) if scale else 0.0)
            return len(self.floors) - 1

    def reset(self):
        """Drop the points but keep the floor/scale tables.

        Used when the clock steps BACKWARDS: the arrays are searched with
        bisect everywhere (query, evict), so a single out-of-order append makes
        every one of those return nonsense - the trail vanishes, and evict
        deletes an arbitrary block instead of the expired head. Losing the
        buffer is the cheap, safe answer to a clock that just lied to us.
        """
        del self.t[:]
        del self.x[:]
        del self.y[:]
        del self.f[:]
        del self.gap[:]
        self.last_kept = None

    def append(self, ts, x, y, fi, gap):
        self.t.append(float(ts))
        self.x.append(float(x))
        self.y.append(float(y))
        self.f.append(fi)
        self.gap.append(1 if gap else 0)
        self.last_kept = (float(ts), float(x), float(y), fi)

    def evict(self, max_age, max_points, now):
        """Drop expired/excess points in BLOCKS (one memmove, not one per point)."""
        n = len(self.t)
        if n == 0:
            return
        drop = bisect.bisect_left(self.t, now - max_age)
        at_cap = n - drop > max_points
        if at_cap:
            # Drop a BLOCK, not the single point that put us over: at the cap
            # `n - max_points` is 1 on every record, which would memmove the
            # whole buffer once per fix - exactly what the amortisation below
            # exists to avoid, in the one regime where it never gets to run.
            drop = max(n - max_points, min(n, max(1, max_points // 64)))
        if drop <= 0:
            return
        # Amortise: compacting on every expiry would memmove the whole buffer
        # once per point. Wait for a block worth ~6% of what we hold (with a
        # small floor so short buffers still expire promptly) unless the point
        # cap is forcing our hand. The threshold scales with `n`, NOT with
        # max_points: a "compact once max_points/10 expired" rule never fires
        # for a ring that holds far fewer points than the cap (6 h at one point
        # per 2 s is ~10k against a 200k cap), so max_age would be ignored and
        # the ring would grow to the cap regardless of the configured window.
        if not at_cap and drop < max(8, n // 16):
            return
        del self.t[:drop]
        del self.x[:drop]
        del self.y[:drop]
        del self.f[:drop]
        del self.gap[:drop]
        if self.gap:
            self.gap[0] = 1       # the new first point starts a polyline


class PositionHistory:
    """Every tracker's history, plus the NDJSON lines waiting to be appended."""

    MAX_PENDING = 50_000          # never let the flush queue grow unbounded

    def __init__(self, cfg=None):
        self.tracks = {}
        self.cfg = cfg or history_config({})
        self._pending = []
        self.dropped_pending = 0

    def configure(self, cfg):
        self.cfg = cfg

    # --- recording ---------------------------------------------------------
    def record(self, ent, ts, x_m, y_m, floor, scale):
        """Keep this fix if it clears the gate. Returns True when kept.

        Kept when the floor changed (always - the two points live in different
        frames), or enough time AND movement has passed, or the heartbeat is
        due so a stationary device still has something to scrub to.
        """
        if not self.cfg.get("enabled", True):
            return False
        if not isinstance(ent, str) or not ent:
            return False
        ts, x_m, y_m = _finite(ts), _finite(x_m), _finite(y_m)
        if ts is None or x_m is None or y_m is None:
            return False
        track = self.tracks.get(ent)
        if track is None:
            track = self.tracks[ent] = _Track()
        fi = track.floor_index(floor, scale)

        gap = False
        last = track.last_kept
        if last is None:
            gap = True
        else:
            lt, lx, ly, lf = last
            if ts < lt:
                # The clock stepped backwards (NTP correction, a VM resume, a
                # host with no RTC catching up). Appending here would leave
                # track.t unsorted and break every bisect that reads it, so
                # start the buffer over rather than corrupt it.
                track.reset()
                fi = track.floor_index(floor, scale)
                gap = True
            elif fi != lf:
                gap = True                  # different pixel frame: break the line
            else:
                dt = ts - lt
                moved = math.hypot(x_m - lx, y_m - ly)
                if not (dt >= self.cfg["heartbeat"]
                        or (dt >= self.cfg["min_interval"]
                            and moved >= self.cfg["min_move_m"])):
                    return False
                # Heard again after a silence: the device was not standing
                # still, it was not being heard, so break the line instead of
                # drawing one long straight segment across the outage. (The
                # tracker only gets an explicit mark_gap once it has been
                # absent for the whole position_timeout, which is much longer.)
                if dt > max(self.cfg["heartbeat"] * DROPOUT_GAP_FACTOR,
                            DROPOUT_GAP_MIN):
                    gap = True
        if track.force_gap:
            gap = True
            track.force_gap = False

        track.append(ts, x_m, y_m, fi, gap)
        self._queue(ent, ts, x_m, y_m, track.floors[fi], track.scales[fi], gap)
        track.evict(self.cfg["max_age"], self.cfg["max_points"], ts)
        return True

    def evict_all(self, now=None):
        """Age every track, not just the one that recorded.

        record() evicts the track it touched, which is enough while a device
        keeps reporting - but a tracker that has gone silent (left the house,
        battery flat), or a history that has since been switched OFF, would
        otherwise keep serving points long past the configured window. Called
        from the periodic flush and before every query.
        """
        now = time.time() if now is None else now
        for track in self.tracks.values():
            track.evict(self.cfg["max_age"], self.cfg["max_points"], now)

    def mark_gap(self, ent):
        """The next point for this tracker starts a new polyline.

        Used when a tracker is pruned for absence, or across a restart, so the
        scrubber does not draw a straight line over a gap in the record.
        """
        track = self.tracks.get(ent)
        if track is None:
            track = self.tracks[ent] = _Track()
        track.force_gap = True

    def forget(self, ent=None):
        """Discard the in-memory record for one tracker, or for all of them.

        Also drops that tracker's queued-but-unflushed rows, which would
        otherwise be appended to disk moments after the clear and reappear on
        the next restart.
        """
        if ent is None:
            self.tracks.clear()
            self._pending = []
            return
        self.tracks.pop(ent, None)
        self._pending = [row for row in self._pending if row[1] != ent]

    def mark_all_gaps(self):
        for track in self.tracks.values():
            track.force_gap = True

    # --- disk hand-off (the caller performs the actual I/O) ----------------
    def _queue(self, ent, ts, x, y, floor, scale, gap):
        if len(self._pending) >= self.MAX_PENDING:
            self.dropped_pending += 1
            return
        row = {"e": ent, "t": round(float(ts), 2), "x": round(float(x), 3),
               "y": round(float(y), 3), "f": floor}
        if scale:
            row["s"] = round(float(scale), 4)
        if gap:
            row["g"] = 1
        # Tagged with its UTC day so the flush can group by segment file, and
        # with the entity so forget() can drop just that tracker's queued rows
        # without having to pattern-match the serialised JSON.
        self._pending.append(
            (day_key(ts), ent, json.dumps(row, separators=(",", ":"))))

    def drain_pending(self):
        """Take the queued rows as {day_key: [line, ...]} for the caller to append."""
        out, self._pending = self._pending, []
        grouped = {}
        for day, _ent, line in out:
            grouped.setdefault(day, []).append(line)
        return grouped

    def pending_count(self):
        return len(self._pending)

    def requeue(self, grouped):
        """Put a drained batch back after a failed write.

        The rows go to the FRONT so they keep their place ahead of anything
        recorded since, and the pending cap still applies - a disk that stays
        broken drops the oldest rows rather than growing without bound.
        """
        back = []
        for day, lines in grouped.items():
            for line in lines:
                ent = ""
                try:
                    ent = json.loads(line).get("e") or ""
                except ValueError:
                    pass
                back.append((day, ent, line))
        if not back:
            return
        self._pending[:0] = back
        if len(self._pending) > self.MAX_PENDING:
            over = len(self._pending) - self.MAX_PENDING
            del self._pending[:over]
            self.dropped_pending += over

    def adopt(self, other):
        """Take another PositionHistory's tracks as our own (restore path).

        Used so the parse/build can happen in an executor and only the handover
        touches the object the event loop reads.
        """
        self.tracks = other.tracks

    def load_rows(self, rows, now=None):
        """Restore from parsed NDJSON rows, ignoring junk rows.

        The rows are sorted by timestamp first: file order is only *usually*
        chronological (a clock step, or a segment written across a restart, can
        break it), and every read of these arrays is a bisect that needs them
        sorted.
        """
        now = time.time() if now is None else now
        cutoff = now - self.cfg["max_age"]
        clean = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ent = row.get("e")
            if not isinstance(ent, str) or not ent:
                continue
            ts = _finite(row.get("t"))
            x, y = _finite(row.get("x")), _finite(row.get("y"))
            if ts is None or x is None or y is None or ts < cutoff:
                continue
            clean.append((ts, ent, x, y, row))
        clean.sort(key=lambda r: r[0])

        restored = 0
        for ts, ent, x, y, row in clean:
            track = self.tracks.get(ent)
            if track is None:
                track = self.tracks[ent] = _Track()
            fi = track.floor_index(row.get("f"), _finite(row.get("s")) or 0.0)
            track.append(ts, x, y, fi, bool(row.get("g")))
            restored += 1
        for track in self.tracks.values():
            track.evict(self.cfg["max_age"], self.cfg["max_points"], now)
        return restored

    # --- querying ----------------------------------------------------------
    def retained(self, ent):
        """The span actually held for one tracker, or None when empty."""
        track = self.tracks.get(ent)
        if track is None or not track.t:
            return None
        return {"from": round(track.t[0], 2), "to": round(track.t[-1], 2),
                "points": len(track.t)}

    def entities(self):
        return sorted(e for e, tr in self.tracks.items() if tr.t)

    def query(self, ent, frm, to, max_points):
        """Points for one tracker in [frm, to], decimated to ~max_points.

        Decimation keeps a uniform stride so the trail spans the WHOLE window,
        and always keeps the first and last point, every gap and every floor
        change - dropping those would silently join unrelated stretches.
        """
        empty = {"ent": ent, "floors": [], "scales": [], "t": [], "x_m": [],
                 "y_m": [], "f": [], "gap": [], "count": 0, "total": 0,
                 "stride": 1}
        track = self.tracks.get(ent)
        if track is None or not track.t:
            return empty
        lo = bisect.bisect_left(track.t, frm)
        hi = bisect.bisect_right(track.t, to)
        total = hi - lo
        if total <= 0:
            return empty
        cap = max(2, int(max_points))
        stride = 1 if total <= cap else -(-total // cap)   # ceil division

        keep = []
        for i in range(lo, hi):
            if (i == lo or i == hi - 1 or track.gap[i]
                    or (i > lo and track.f[i] != track.f[i - 1])
                    or (i - lo) % stride == 0):
                keep.append(i)
        # Breaks and floor changes are force-kept above, so data that flaps
        # between floors (or a device pruned and re-seen over and over) can
        # defeat the stride entirely and return the whole buffer - megabytes of
        # JSON for a request that asked for a few thousand points. Thin the
        # result uniformly if that happened; the endpoints and the ordering
        # survive, some breaks do not.
        hard_cap = cap * 2
        if len(keep) > hard_cap:
            step = -(-len(keep) // hard_cap)
            thinned = keep[::step]
            if thinned[-1] != keep[-1]:
                thinned.append(keep[-1])
            keep = thinned
            stride = max(stride, step)

        return {
            "ent": ent,
            "floors": list(track.floors),
            "scales": list(track.scales),
            "t": [round(track.t[i], 2) for i in keep],
            "x_m": [round(track.x[i], 3) for i in keep],
            "y_m": [round(track.y[i], 3) for i in keep],
            "f": [track.f[i] for i in keep],
            "gap": [track.gap[i] for i in keep],
            "count": len(keep),
            "total": total,
            "stride": stride,
        }


def day_key(ts):
    """UTC day stamp used for an on-disk segment name."""
    return time.strftime("%Y%m%d", time.gmtime(ts))


# Segments are whole UTC days, so the day containing (now - max_age) still
# holds in-window rows and must be kept. Both the pruner and the restore use
# this one boundary, so nothing is kept that is never read back.
def oldest_live_day(max_age, now=None):
    now = time.time() if now is None else now
    return day_key(now - max_age)


def expired_day_keys(existing, max_age, now=None):
    """Which day segments are entirely older than the retention window."""
    keep_from = oldest_live_day(max_age, now)
    return sorted(k for k in existing if k < keep_from)


# --- on-disk day segments ----------------------------------------------------
# Plain synchronous file I/O, kept here so the module stays self-contained; the
# caller runs these in an executor. Appends only: a partial write can lose the
# tail of one segment, never the whole history.

def segment_path(dirpath, day):
    return os.path.join(dirpath, "%s.jsonl" % day)


def append_segments(dirpath, grouped):
    """Append {day_key: [line, ...]} to the matching segment files.

    Returns the number of lines written. Creates the directory on demand.
    """
    if not grouped:
        return 0
    os.makedirs(dirpath, exist_ok=True)
    written = 0
    for day, lines in grouped.items():
        if not lines:
            continue
        path = segment_path(dirpath, day)
        # Binary append, and check the last byte first: a previous append that
        # died mid-write leaves a line with no trailing newline, and appending
        # straight onto it would fuse that torn tail and our first row into one
        # unparseable line -- costing a good row as well as the bad one.
        with open(path, "a+b") as fh:
            if fh.tell():
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) not in (b"\n", b"\r"):
                    fh.write(b"\n")
                fh.seek(0, os.SEEK_END)
            fh.write(("\n".join(lines) + "\n").encode("utf-8"))
        written += len(lines)
    return written


def list_day_keys(dirpath):
    """Day keys of the segments currently on disk, oldest first."""
    try:
        names = os.listdir(dirpath)
    except OSError:
        return []
    out = []
    for name in names:
        if name.endswith(".jsonl") and len(name) == len("YYYYMMDD.jsonl"):
            stem = name[:-len(".jsonl")]
            if stem.isdigit():
                out.append(stem)
    return sorted(out)


def read_segments(dirpath, days):
    """Parsed rows from the named segments, oldest first.

    A torn final line (power cut mid-append) is skipped rather than aborting
    the restore, so one bad line cannot cost the rest of the history.
    """
    rows = []
    for day in days:
        path = segment_path(dirpath, day)
        try:
            # errors="replace": a corrupt byte would otherwise raise
            # UnicodeDecodeError from inside the iteration - which is NOT an
            # OSError, so it would escape this guard and cost the entire
            # restore. Mangled text just fails to parse as JSON below.
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue          # torn/garbled line: skip it
        except OSError:
            continue
    return rows


def prune_segments(dirpath, max_age, now=None):
    """Delete segments entirely older than the retention window."""
    _sweep_temps(dirpath)   # a rewrite that died leaves a full copy behind
    removed = []
    for day in expired_day_keys(list_day_keys(dirpath), max_age, now):
        try:
            os.remove(segment_path(dirpath, day))
            removed.append(day)
        except OSError:
            pass
    return removed


def restore_recent(dirpath, cfg, now=None):
    """Rows from the segments that can still be inside the retention window."""
    oldest = oldest_live_day(cfg["max_age"], now)
    days = [d for d in list_day_keys(dirpath) if d >= oldest]
    return read_segments(dirpath, days)


def drop_entity(dirpath, ent, cfg, now=None):
    """Rewrite every segment without one tracker's rows. Returns rows removed.

    Segments are shared by all trackers, so forgetting one means a rewrite.
    Each file is written to a sibling temp and os.replace()d, so a crash
    mid-rewrite leaves the original segment intact rather than a truncated one.
    """
    removed = 0
    for day in list_day_keys(dirpath):
        path = segment_path(dirpath, day)
        tmp = path + ".tmp"
        kept = 0
        try:
            with open(path, encoding="utf-8", errors="replace") as src, \
                    open(tmp, "w", encoding="utf-8") as dst:
                for line in src:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        row = json.loads(stripped)
                    except ValueError:
                        continue          # torn line: dropping it is the repair
                    if isinstance(row, dict) and row.get("e") == ent:
                        removed += 1
                        continue
                    dst.write(stripped + "\n")
                    kept += 1
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            continue
        try:
            if kept:
                os.replace(tmp, path)
            else:
                os.remove(tmp)            # nothing left: drop the segment
                os.remove(path)
        except OSError:
            pass
    return removed


def clear_segments(dirpath):
    """Delete every segment. Returns the number of files removed.

    Also sweeps orphaned .tmp files: list_day_keys deliberately ignores them,
    so a rewrite that died mid-flight would otherwise leave a copy of the
    record on disk that nothing - not Clear, not the pruner - ever removes.
    """
    removed = 0
    for day in list_day_keys(dirpath):
        try:
            os.remove(segment_path(dirpath, day))
            removed += 1
        except OSError:
            pass
    removed += _sweep_temps(dirpath)
    return removed


def _sweep_temps(dirpath):
    """Remove leftover .tmp segment files. Returns how many went."""
    removed = 0
    try:
        names = os.listdir(dirpath)
    except OSError:
        return 0
    for name in names:
        if name.endswith(".jsonl.tmp"):
            try:
                os.remove(os.path.join(dirpath, name))
                removed += 1
            except OSError:
                pass
    return removed


def disk_usage(dirpath):
    """(files, bytes) currently used by the segments, for diagnostics."""
    files = list_day_keys(dirpath)
    total = 0
    for day in files:
        try:
            total += os.path.getsize(segment_path(dirpath, day))
        except OSError:
            pass
    return len(files), total
