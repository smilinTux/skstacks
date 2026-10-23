"""versions.extract/at_least must parse plain dotted-integer versions out of
image tags and advisory strings, compare them correctly, and never guess on
anything that doesn't parse that way."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

from versions import at_least, extract  # noqa: E402


def test_extract_default_pattern_finds_first_dotted_version():
    assert extract("v26.09.2-ls292") == "26.09.2"
    assert extract("traefik:v3.6.2") == "3.6.2"


def test_extract_with_capture_group_pattern():
    assert extract("v3.6.2", pattern=r"v?(\d+\.\d+\.\d+)") == "3.6.2"


def test_extract_returns_none_for_no_match_or_empty():
    assert extract("latest") is None
    assert extract("") is None
    assert extract(None) is None


def test_at_least_true_when_equal_or_above():
    assert at_least("3.7.14", "3.7.14") is True
    assert at_least("3.7.16", "3.7.14") is True


def test_at_least_false_when_below():
    assert at_least("3.6.2", "3.7.14") is False
    assert at_least("1.837.0", "1.844.0") is False


def test_at_least_none_when_unparsable():
    assert at_least("latest", "3.7.14") is None
    assert at_least("3.7.14", None) is None
