import sys

from _pytest.runner import CallInfo

from flakytest.profiling.markers import MARKER_FILENAME, PhaseMarkers, make_marker, marker_key, parse_marker


def test_marker_frame_is_named_after_phase_and_test():
    seen = {}

    def inner():
        frame = sys._getframe(1)
        seen["name"] = frame.f_code.co_name
        seen["file"] = frame.f_code.co_filename
        return 42

    key = marker_key("tests/test_a.py::test_b")
    assert make_marker("call", key)(inner) == 42
    assert seen == {"name": f"__flakytest_call_{key}__", "file": MARKER_FILENAME}
    assert parse_marker(seen["file"], seen["name"]) == ("call", key)


def test_parse_marker_ignores_other_frames():
    assert parse_marker("tests/test_a.py", "__flakytest_call_abc__") is None
    assert parse_marker(MARKER_FILENAME, "something_else") is None
    assert parse_marker(MARKER_FILENAME, "__flakytest_collect__") == ("collect", None)


def test_marker_key_is_stable_and_short():
    assert marker_key("a::b") == marker_key("a::b")
    assert marker_key("a::b") != marker_key("a::c")
    assert len(marker_key("a::b")) == 12


def test_phase_markers_wrap_from_call_and_restore_it():
    original = CallInfo.from_call
    markers = PhaseMarkers()
    markers.install()
    try:
        markers.current_key = "0123456789ab"
        names = []

        def runtest():
            frame = sys._getframe(1)
            while frame is not None:
                names.append(frame.f_code.co_name)
                frame = frame.f_back

        info = CallInfo.from_call(runtest, when="call")
        assert info.excinfo is None
        # (when this suite is itself profiled, the markers of its own tests are on the stack too)
        assert "__flakytest_call_0123456789ab__" in names
    finally:
        markers.uninstall()
    assert CallInfo.from_call == original
