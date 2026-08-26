from __future__ import annotations

from evals.routing_report import build_report, render_text


def _trace(**overrides) -> dict:
    base = {
        "requested_model": "fast",
        "actual_model": "p1/m1",
        "latency_ms": 100,
        "cost_usd": 0.001,
        "attempts": 1,
        "status": "success",
        "error_code": None,
    }
    base.update(overrides)
    return base


def test_build_report_aggregates_modes_targets_and_failures():
    report = build_report(
        [
            _trace(),
            _trace(attempts=3, actual_model="p2/m2", latency_ms=300, cost_usd=0.002),
            _trace(status="failed", actual_model=None, error_code="model_unavailable", attempts=4),
        ]
    )
    fast = report["modes"]["fast"]
    assert fast["requests"] == 3
    assert fast["success_rate"] == round(2 / 3, 3)
    assert fast["fallback_rate"] == round(2 / 3, 3)  # attempts>1 的两条
    assert set(fast["targets"]) == {"p1/m1", "p2/m2"}
    assert fast["targets"]["p1/m1"]["requests"] == 1
    assert report["failures"] == {"model_unavailable": 1}


def test_render_text_outputs_readable_summary():
    report = build_report([_trace(), _trace(attempts=2)])
    text = render_text(report)
    assert "档位 fast" in text
    assert "fallback 率 50.0%" in text
