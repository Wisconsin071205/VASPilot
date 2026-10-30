"""Machine-readable JSON must stay UTF-8 even in frozen Windows apps."""
import io
import json

from rsci.core.jsonio import emit


def test_json_pipe_ignores_windows_locale_encoding():
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk")
    emit({"ok": True, "message": "科研结果：α 😀"}, stream=stream)
    assert json.loads(raw.getvalue().decode("utf-8"))["message"] == "科研结果：α 😀"


def test_text_only_capture_remains_supported():
    stream = io.StringIO()
    emit({"ok": True, "message": "科研"}, stream=stream)
    assert json.loads(stream.getvalue())["message"] == "科研"
