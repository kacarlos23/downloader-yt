import pytest

from downloader_youtube import (
    build_time_range,
    format_time,
    parse_ffmpeg_progress_time,
    parse_timecode,
    time_range_duration,
    validate_youtube_url,
)


class TestParseTimecode:
    @pytest.mark.parametrize(
        ("input_val", "expected"),
        [
            ("90", 90.0),
            ("01:30", 90.0),
            ("00:10:15", 615.0),
            ("fim", float("inf")),
            ("final", float("inf")),
        ],
    )
    def test_valid_formats(self, input_val: str, expected: float) -> None:
        assert parse_timecode(input_val, label="--test") == expected

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="formato|invalido"):
            parse_timecode("invalido", label="--test")

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="negativo"):
            parse_timecode("-1", label="--test")


class TestBuildTimeRange:
    def test_none_without_bounds(self) -> None:
        assert build_time_range(None, None) is None

    def test_start_and_end(self) -> None:
        assert build_time_range("01:00", "02:00") == (60.0, 120.0)

    def test_invalid_order_raises(self) -> None:
        with pytest.raises(ValueError, match="maior"):
            build_time_range("02:00", "01:00")

    def test_duration(self) -> None:
        assert time_range_duration((10.0, 15.5)) == 5.5

    def test_infinite_duration_is_none(self) -> None:
        assert time_range_duration((10.0, float("inf"))) is None


class TestFormatTime:
    def test_infinity(self) -> None:
        assert format_time(float("inf")) == "fim"

    def test_seconds_only(self) -> None:
        assert format_time(90) == "01:30"

    def test_with_hours(self) -> None:
        assert format_time(3665) == "01:01:05"


class TestParseFfmpegProgressTime:
    def test_extracts_progress_time(self) -> None:
        assert parse_ffmpeg_progress_time("size= 123kB time=00:01:02.50 bitrate=16.1kbits/s") == 62.5

    def test_missing_progress_time(self) -> None:
        assert parse_ffmpeg_progress_time("sem progresso") is None


class TestValidateYoutubeUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/@canal",
        ],
    )
    def test_valid_youtube_urls(self, url: str) -> None:
        assert validate_youtube_url(url)

    def test_rejects_non_youtube_url(self) -> None:
        assert not validate_youtube_url("https://example.com/video")
