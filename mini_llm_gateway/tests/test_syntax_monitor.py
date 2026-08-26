from __future__ import annotations

import pytest

from mini_llm_gateway.service.syntax_monitor import JsonSyntaxBroken, JsonSyntaxMonitor


def feed_all(monitor: JsonSyntaxMonitor, *deltas: str) -> None:
    for delta in deltas:
        monitor.feed(delta)


def test_valid_json_stream_never_breaks():
    monitor = JsonSyntaxMonitor()
    feed_all(monitor, '{"a": ', '[1, ', '"x{"', ', ', '{"b": null}]', '}')
    assert monitor.balanced is True


def test_string_braces_do_not_count():
    monitor = JsonSyntaxMonitor()
    feed_all(monitor, '{"k": "va]lue{with braces"}')
    assert monitor.balanced is True


def test_escaped_quote_inside_string():
    monitor = JsonSyntaxMonitor()
    feed_all(monitor, '{"k": "say \\"hi\\""}')
    assert monitor.balanced is True


def test_state_persists_across_deltas():
    monitor = JsonSyntaxMonitor()
    monitor.feed('{"k": "abc')
    monitor.feed('def')  # 跨增量仍在字符串里，花括号字符不计数
    monitor.feed('"}')
    assert monitor.balanced is True


def test_mismatched_bracket_breaks():
    monitor = JsonSyntaxMonitor()
    with pytest.raises(JsonSyntaxBroken):
        feed_all(monitor, '{"a": [1}]')


def test_unmatched_close_breaks():
    monitor = JsonSyntaxMonitor()
    with pytest.raises(JsonSyntaxBroken):
        feed_all(monitor, '{"a": 1}}')


def test_trailing_content_after_root_breaks():
    monitor = JsonSyntaxMonitor()
    with pytest.raises(JsonSyntaxBroken):
        feed_all(monitor, '{"a": 1} 再见')


def test_trailing_whitespace_after_root_ok():
    monitor = JsonSyntaxMonitor()
    feed_all(monitor, '{"a": 1} \n')
    assert monitor.balanced is True


def test_unclosed_root_not_balanced():
    monitor = JsonSyntaxMonitor()
    feed_all(monitor, '{"a": [1, 2')
    assert monitor.balanced is False  # 未破损但不完整，交给流尾判定
