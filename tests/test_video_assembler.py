"""Tests for the video assembler (all mocked — no real ffmpeg calls)."""

import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock, mock_open

from services.video_assembler import VideoAssembler, SLIDE_WIDTH, SLIDE_HEIGHT


@pytest.fixture
def assembler():
    return VideoAssembler()


class TestCreateSlides:
    def test_creates_title_slide(self, assembler):
        sections = [
            {
                "type": "title",
                "headline": "CropSight Intelligence Signal",
                "week_label": "WEEK 14 / 2026",
                "subtitle": "A big week for commodities.",
            }
        ]

        slides = assembler.create_slides(sections)

        assert len(slides) == 1
        # Should be valid PNG bytes
        assert slides[0][:4] == b"\x89PNG"

    def test_creates_section_slide(self, assembler):
        sections = [
            {
                "type": "section",
                "headline": "Commodity Pulse",
                "bullets": ["Wheat up 4%", "Coffee down 2%"],
            }
        ]

        slides = assembler.create_slides(sections)

        assert len(slides) == 1
        assert slides[0][:4] == b"\x89PNG"

    def test_creates_closing_slide(self, assembler):
        sections = [
            {
                "type": "closing",
                "subtitle": "Read the full report.",
            }
        ]

        slides = assembler.create_slides(sections)

        assert len(slides) == 1
        assert slides[0][:4] == b"\x89PNG"

    def test_creates_multiple_slides(self, assembler):
        sections = [
            {"type": "title", "headline": "Title", "week_label": "W14"},
            {"type": "section", "headline": "Section 1", "bullets": ["Item 1"]},
            {"type": "section", "headline": "Section 2", "bullets": ["Item 2"]},
            {"type": "closing", "subtitle": "End."},
        ]

        slides = assembler.create_slides(sections)

        assert len(slides) == 4
        for s in slides:
            assert s[:4] == b"\x89PNG"

    def test_handles_empty_sections(self, assembler):
        slides = assembler.create_slides([])

        assert slides == []

    def test_slide_dimensions(self, assembler):
        from PIL import Image
        import io

        sections = [{"type": "title", "headline": "Test"}]
        slides = assembler.create_slides(sections)

        img = Image.open(io.BytesIO(slides[0]))
        assert img.size == (SLIDE_WIDTH, SLIDE_HEIGHT)


class TestParseScriptToSections:
    def test_creates_title_and_closing(self, assembler):
        script = "Big week for AgTech. Wheat prices rose. Coffee dropped. Read the full report."

        sections = assembler.parse_script_to_sections(script)

        assert sections[0]["type"] == "title"
        assert sections[-1]["type"] == "closing"

    def test_caps_at_8_slides(self, assembler):
        # Long script with many sentences
        script = ". ".join([f"Sentence number {i}" for i in range(30)]) + "."

        sections = assembler.parse_script_to_sections(script)

        assert len(sections) <= 8

    def test_includes_week_label(self, assembler):
        script = "First sentence. Second sentence. Closing."

        sections = assembler.parse_script_to_sections(script)

        assert "WEEK" in sections[0].get("week_label", "")

    def test_handles_empty_script(self, assembler):
        sections = assembler.parse_script_to_sections("")

        assert len(sections) >= 2  # At least title + closing


class TestAssembleVideo:
    @pytest.mark.asyncio
    async def test_returns_none_with_no_slides(self, assembler):
        result = await assembler.assemble_video([], b"audio")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_with_no_audio(self, assembler):
        result = await assembler.assemble_video([b"slide"], b"")

        assert result is None

    @pytest.mark.asyncio
    async def test_handles_ffmpeg_not_found(self, assembler):
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("ffmpeg not found")):
            result = await assembler.assemble_video(
                [b"\x89PNG" + b"\x00" * 100],
                b"audio-data",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_handles_ffmpeg_failure(self, assembler):
        mock_process = AsyncMock()
        mock_process.returncode = 1
        mock_process.communicate = AsyncMock(return_value=(b"", b"Error occurred"))

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(assembler, "_get_audio_duration", return_value=30.0):
                result = await assembler.assemble_video(
                    [b"\x89PNG" + b"\x00" * 100],
                    b"audio-data",
                )

        assert result is None

    @pytest.mark.asyncio
    async def test_handles_ffmpeg_timeout(self, assembler):
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            with patch.object(assembler, "_get_audio_duration", return_value=30.0):
                result = await assembler.assemble_video(
                    [b"\x89PNG" + b"\x00" * 100],
                    b"audio-data",
                )

        assert result is None


class TestGetAudioDuration:
    @pytest.mark.asyncio
    async def test_returns_duration(self, assembler):
        mock_process = AsyncMock()
        mock_process.communicate = AsyncMock(
            return_value=(b"45.5\n", b"")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_process):
            duration = await assembler._get_audio_duration("/tmp/audio.mp3")

        assert duration == 45.5

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self, assembler):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("ffprobe not found"),
        ):
            duration = await assembler._get_audio_duration("/tmp/audio.mp3")

        assert duration is None


class TestWrapText:
    def test_wraps_long_text(self, assembler):
        text = "This is a very long sentence that should be wrapped to fit the slide width properly"
        lines = assembler._wrap_text(text, max_chars=40)

        assert len(lines) >= 2
        for line in lines:
            assert len(line) <= 45  # Some slack for word boundaries

    def test_short_text_single_line(self, assembler):
        text = "Short text"
        lines = assembler._wrap_text(text, max_chars=40)

        assert len(lines) == 1
        assert lines[0] == "Short text"

    def test_empty_text(self, assembler):
        lines = assembler._wrap_text("", max_chars=40)

        assert lines == [""]
