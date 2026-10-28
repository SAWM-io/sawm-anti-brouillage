"""Causal, bounded selection of application paths from verified receipts.

This policy learns request/ACK behaviour, not spectrum occupancy. The caller
supplies a monotonic clock and verifies receipts before reporting success. No
network, radio model, fault schedule, random source or Cloud call is used here.
Timeouts are censored failures: their duration is never a successful RTT sample.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re


ALGORITHM = "causal-multipath-ewma/v1"


def _number(value, name, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError("invalid_" + name)
    return float(value)


class MultipathSelector:
    """Serial selector with cooldown, hysteresis and bounded recovery probes.

    Call ``select(now_ms)``, wait ``last_decision['wait_ms']`` if nonzero,
    then report the attempt with ``observe``. A caller-owned deadline and retry
    budget remain necessary: no algorithm can deliver through a total outage.
    A periodic probe spends one attempt, so there is no claim of zero interruption.
    """

    def __init__(self, paths=("a", "b", "c", "d"), *, initial_timeout_ms=350,
                 min_timeout_ms=150, max_timeout_ms=1000, cooldown_ms=350,
                 max_cooldown_ms=1500, probe_every=12, hysteresis=0.20):
        paths = tuple(paths)
        if not 2 <= len(paths) <= 32 or len(set(paths)) != len(paths) or any(
                not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", p) for p in paths):
            raise ValueError("invalid_paths")
        for name, value in (("initial_timeout_ms", initial_timeout_ms),
                            ("min_timeout_ms", min_timeout_ms), ("max_timeout_ms", max_timeout_ms),
                            ("cooldown_ms", cooldown_ms), ("max_cooldown_ms", max_cooldown_ms)):
            _number(value, name, 1)
        if not 25 <= min_timeout_ms <= initial_timeout_ms <= max_timeout_ms <= 10000:
            raise ValueError("invalid_timeout_bounds")
        if not 1 <= cooldown_ms <= max_cooldown_ms <= 30000:
            raise ValueError("invalid_cooldown_bounds")
        if type(probe_every) is not int or not 2 <= probe_every <= 1000:
            raise ValueError("invalid_probe_interval")
        if not 0 <= _number(hysteresis, "hysteresis") <= 0.8:
            raise ValueError("invalid_hysteresis")
        self.paths = paths
        self.limits = dict(initial_timeout_ms=initial_timeout_ms, min_timeout_ms=min_timeout_ms,
                           max_timeout_ms=max_timeout_ms, cooldown_ms=cooldown_ms,
                           max_cooldown_ms=max_cooldown_ms, probe_every=probe_every,
                           hysteresis=hysteresis)
        self.state = {p: dict(attempts=0, verified_acks=0, consecutive_failures=0,
                             srtt_ms=None, rttvar_ms=None, success_ewma=1.0,
                             timeout_ms=initial_timeout_ms, cooldown_until_ms=0.0,
                             last_observation_ms=None) for p in paths}
        self.current = paths[0]
        self.selections = 0
        self.last_probe_selection = 0
        self.last_decision = None
        self.last_time_ms = 0.0

    def _time(self, now_ms):
        now_ms = _number(now_ms, "clock")
        if now_ms < self.last_time_ms:
            raise ValueError("clock_went_backwards")
        self.last_time_ms = now_ms
        return now_ms

    def _path(self, path):
        if path not in self.state:
            raise ValueError("unknown_path")
        return self.state[path]

    def _score(self, path):
        row = self.state[path]
        # A small common nominal prior breaks no ties: configured order does.
        rtt = row["srtt_ms"] if row["srtt_ms"] is not None else self.limits["initial_timeout_ms"] / 2
        return rtt + self.limits["initial_timeout_ms"] * (
            2 * (1 - row["success_ewma"]) + row["consecutive_failures"])

    def request_timeout_ms(self, path):
        return math.ceil(self._path(path)["timeout_ms"])

    def retry_delay_ms(self, now_ms):
        """Minimum wait until one path is eligible; does not change the state."""
        now_ms = _number(now_ms, "clock")
        return max(0, math.ceil(min(s["cooldown_until_ms"] for s in self.state.values()) - now_ms))

    def select(self, now_ms):
        now_ms = self._time(now_ms)
        eligible = [p for p in self.paths if self.state[p]["cooldown_until_ms"] <= now_ms]
        wait_ms = 0
        if not eligible:
            # Give the least recently failed route its scheduled retry, rather
            # than spin or repeatedly hit the previous route during total loss.
            earliest = min(s["cooldown_until_ms"] for s in self.state.values())
            eligible = [p for p in self.paths if self.state[p]["cooldown_until_ms"] == earliest]
            wait_ms = max(0, math.ceil(earliest - now_ms))
            reason = "all_paths_cooling_retry_earliest"
        else:
            reason = "lowest_observed_cost"
        best = min(eligible, key=self._score)
        current_ok = self.current in eligible and not self.state[self.current]["consecutive_failures"]
        if current_ok and self._score(self.current) <= self._score(best) / (1 - self.limits["hysteresis"]):
            chosen = self.current
            reason = "keep_path_with_hysteresis"
        else:
            chosen = best
            if not current_ok and self.selections:
                reason = "failed_path_bypass" if wait_ms == 0 else reason
        # Probe one alternative only after a bounded number of selections.
        # Least-recent observation makes every route eventually eligible for
        # recovery; first unseen paths are explored in configured order.
        alternatives = [p for p in eligible if p != chosen]
        probe = bool(alternatives and self.selections - self.last_probe_selection >= self.limits["probe_every"])
        if probe:
            chosen = min(alternatives, key=lambda p: (
                self.state[p]["last_observation_ms"] is not None,
                self.state[p]["last_observation_ms"] or 0,
                self.paths.index(p)))
            self.last_probe_selection = self.selections
            reason = "bounded_recovery_probe"
        previous = self.current
        # A probe does not replace the incumbent just because it was attempted.
        if not probe:
            self.current = chosen
        self.selections += 1
        self.last_decision = dict(algorithm=ALGORITHM, selected_path=chosen,
                                 previous_path=previous, reason=reason, now_ms=now_ms,
                                 wait_ms=wait_ms, eligible_paths=list(eligible),
                                 scores={p: round(self._score(p), 6) for p in self.paths},
                                 selection=self.selections, uses_future_schedule=False)
        return chosen

    def observe(self, path, success, latency_ms, now_ms):
        row = self._path(path)
        if type(success) is not bool:
            raise ValueError("success_must_be_verified_boolean")
        latency_ms = _number(latency_ms, "latency")
        now_ms = self._time(now_ms)
        row["attempts"] += 1
        row["last_observation_ms"] = now_ms
        row["success_ewma"] = 0.75 * row["success_ewma"] + 0.25 * int(success)
        if success:
            sample = min(latency_ms, 3 * self.limits["max_timeout_ms"])
            if row["srtt_ms"] is None:
                row["srtt_ms"], row["rttvar_ms"] = sample, sample / 2
            else:
                row["rttvar_ms"] = 0.75 * row["rttvar_ms"] + 0.25 * abs(row["srtt_ms"] - sample)
                row["srtt_ms"] = 0.875 * row["srtt_ms"] + 0.125 * sample
            row["timeout_ms"] = min(self.limits["max_timeout_ms"], max(
                self.limits["min_timeout_ms"], math.ceil(row["srtt_ms"] + 4 * row["rttvar_ms"] + 25)))
            row["consecutive_failures"] = 0
            row["cooldown_until_ms"] = now_ms
            row["verified_acks"] += 1
        else:
            row["consecutive_failures"] += 1
            multiplier = 2 ** min(row["consecutive_failures"] - 1, 10)
            row["cooldown_until_ms"] = now_ms + min(
                self.limits["max_cooldown_ms"], self.limits["cooldown_ms"] * multiplier)
            row["timeout_ms"] = min(self.limits["max_timeout_ms"], max(
                self.limits["initial_timeout_ms"], row["timeout_ms"] * 1.5))
        return deepcopy(row)

    def snapshot(self):
        return deepcopy(dict(algorithm=ALGORITHM, paths=list(self.paths), limits=self.limits,
                             current_path=self.current, selections=self.selections,
                             last_probe_selection=self.last_probe_selection,
                             last_time_ms=self.last_time_ms, last_decision=self.last_decision,
                             state=self.state))
