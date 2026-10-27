"""Bounded, deterministic HTTP retry calibration from completed observations only.

This estimates request/ACK latency, not radio conditions. A timeout is a
censored observation: it raises the timeout but never enters the RTT estimator.
All immutable limits come from the queue's original policy and deadline.
Closed connections leave delivery ambiguous and are insufficient evidence of
congestion. Algorithm revisions are pinned per queue for reproducible replay.
"""
from __future__ import annotations

import math


class InvalidAdaptiveState(ValueError):
    pass


STATE_SCHEMA = "resilience-adaptive-state/v1"
EVENT_SCHEMA = "resilience-calibration-event/v1"
LEGACY_ALGORITHM = "bounded-rto/v1"
ALGORITHM = "bounded-rto/v2"
ERRORS = {"connection_refused", "timeout", "connection_closed_without_ack",
          "invalid_ack", "http_protocol_error", "transport_failure", "http_status_not_success"}


def _require(condition):
    if not condition:
        raise InvalidAdaptiveState("adaptive_state_invalid")


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def initial_state(metadata):
    policy = metadata["policy"]
    algorithm = metadata.get("adaptive_algorithm", LEGACY_ALGORITHM)
    _require(algorithm in (LEGACY_ALGORITHM, ALGORITHM))
    limits = {"policy": dict(policy), "origin_monotonic_ns": metadata["origin_monotonic_ns"],
              "deadline_monotonic_ns": metadata["deadline_monotonic_ns"],
              "planned_messages": metadata["planned_messages"]}
    if "adaptive_algorithm" in metadata:
        limits["algorithm"] = algorithm
    return {"schema": STATE_SCHEMA, "limits": limits,
            "request_timeout_ms": policy["request_timeout_ms"], "retry_delay_ms": policy["base_delay_ms"],
            "srtt_ms": None, "rttvar_ms": None, "observed_attempts": 0, "verified_acks": 0,
            "timeout_observations": 0, "consecutive_failures": 0}


def validate_state(metadata, state):
    expected = initial_state(metadata)
    _require(type(state) is dict and set(state) == set(expected))
    _require(state["schema"] == STATE_SCHEMA and state["limits"] == expected["limits"])
    policy = metadata["policy"]
    capacity = metadata["planned_messages"] * policy["max_attempts"]
    for key in ("observed_attempts", "verified_acks", "timeout_observations", "consecutive_failures"):
        _require(type(state[key]) is int and 0 <= state[key] <= capacity)
    _require(state["verified_acks"] + state["timeout_observations"] <= state["observed_attempts"])
    _require(state["consecutive_failures"] <= state["observed_attempts"] - state["verified_acks"])
    for key, low, high in (("request_timeout_ms", 50, policy["request_timeout_ms"]),
                           ("retry_delay_ms", 25, policy["max_delay_ms"])):
        _require(type(state[key]) is int and low <= state[key] <= high)
    if state["verified_acks"] == 0:
        _require(state["srtt_ms"] is None and state["rttvar_ms"] is None)
    else:
        for key in ("srtt_ms", "rttvar_ms"):
            _require(_number(state[key], 0, 3 * policy["request_timeout_ms"]))
    return state


def observe(metadata, state, result, attempt_id):
    """Return new state and a reproducible trace; the caller persists atomically.

    The caller must have verified the ACK before supplying ack_verified=True.
    Invalid ACKs back off retries but never update latency or decrease timeout.
    No clock, future schedule, network access, or random source is used here.
    """
    validate_state(metadata, state)
    _require(type(attempt_id) is int and attempt_id > 0)
    _require(type(result.get("ack_verified")) is bool)
    duration = result.get("duration_ms")
    _require(_number(duration, 0, float("inf")))
    success, error = result["ack_verified"], result.get("error")
    _require((success and error is None) or (not success and error in ERRORS))
    policy = metadata["policy"]
    updated = {**state, "limits": state["limits"]}
    updated["observed_attempts"] += 1
    censored = None
    if success:
        # Bound estimator inputs even when the OS suspends a request process.
        sample = min(float(duration), 3 * policy["request_timeout_ms"])
        if state["srtt_ms"] is None:
            srtt, variance = sample, sample / 2
        else:
            variance = 0.75 * state["rttvar_ms"] + 0.25 * abs(state["srtt_ms"] - sample)
            srtt = 0.875 * state["srtt_ms"] + 0.125 * sample
        updated.update(srtt_ms=srtt, rttvar_ms=variance,
                       request_timeout_ms=min(policy["request_timeout_ms"], max(50, math.ceil(srtt + 4 * variance + 25))),
                       retry_delay_ms=max(25, math.ceil(state["retry_delay_ms"] / 2)),
                       verified_acks=state["verified_acks"] + 1, consecutive_failures=0)
        reason = "verified_ack_rtt"
    else:
        updated["consecutive_failures"] += 1
        updated["retry_delay_ms"] = min(policy["max_delay_ms"], max(policy["base_delay_ms"], state["retry_delay_ms"] * 2))
        reason = "failure_backoff"
        if error == "connection_closed_without_ack" and metadata.get("adaptive_algorithm", LEGACY_ALGORITHM) == ALGORITHM:
            # An established connection ending without an ACK is insufficient
            # evidence of congestion. Retry at the configured baseline, preserving
            # any larger backoff already justified by other failures. Independent
            # lost ACKs must not compound a queue-wide exponential delay.
            updated["retry_delay_ms"] = max(policy["base_delay_ms"], state["retry_delay_ms"])
            reason = "ambiguous_ack_no_rtt_sample"
        elif error == "timeout":
            censored = state["request_timeout_ms"]
            updated["timeout_observations"] += 1
            updated["request_timeout_ms"] = min(policy["request_timeout_ms"], state["request_timeout_ms"] * 2)
            reason = "timeout_censored_backoff"
        elif error == "invalid_ack":
            reason = "invalid_ack_no_rtt_sample"
    validate_state(metadata, updated)
    event = {"schema": EVENT_SCHEMA, "attempt_id": attempt_id, "reason": reason,
             "observed_error": error, "verified_rtt_ms": float(duration) if success else None,
             "timeout_censored_ms": censored,
             "old_request_timeout_ms": state["request_timeout_ms"],
             "new_request_timeout_ms": updated["request_timeout_ms"],
             "old_retry_delay_ms": state["retry_delay_ms"], "new_retry_delay_ms": updated["retry_delay_ms"],
             "metrics": {key: updated[key] for key in ("srtt_ms", "rttvar_ms", "observed_attempts", "verified_acks",
                                                        "timeout_observations", "consecutive_failures")}}
    if "adaptive_algorithm" in metadata:
        event["algorithm"] = metadata["adaptive_algorithm"]
    return updated, event
