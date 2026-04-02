"""
Video assembler for Intelligence Signal news flash videos.

Creates a short news-flash style video from slide images and audio narration.
Uses PIL for slide rendering and ffmpeg for video assembly.

Built disabled — only active when INTELLIGENCE_SIGNAL_VIDEO_ENABLED=True.

Design:
- 1920x1080 slides, dark navy background (#0A1628)
- CropSight green accents (#00D4AA)
- Inter font (bundled in assets/fonts/, graceful fallback)
- 6-8 slides: title, content sections, closing
- Each slide shown for audio_duration / num_slides seconds

Usage:
    from services.video_assembler import video_assembler

    slides = video_assembler.create_slides(script_sections)
    video_bytes = await video_assembler.assemble_video(slides, audio_bytes)
"""

import asyncio
import io
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# matplotlib MUST use Agg backend before any other import (headless, no display)
import matplotlib
matplotlib.use("Agg")

logger = logging.getLogger(__name__)

# ── Design constants ───────────────────────────────────────────────────

SLIDE_WIDTH = 1920
SLIDE_HEIGHT = 1080

BG_COLOR = (10, 22, 40)        # Dark navy #0A1628
ACCENT_COLOR = (0, 212, 170)   # CropSight green #00D4AA
TEXT_COLOR = (255, 255, 255)    # White
MUTED_COLOR = (160, 170, 185)  # Light grey for secondary text

FONT_DIR = Path(__file__).parent.parent / "assets" / "fonts"
FONT_REGULAR = FONT_DIR / "Inter-Regular.ttf"
FONT_BOLD = FONT_DIR / "Inter-Bold.ttf"

IMAGES_DIR = Path(__file__).parent.parent / "assets" / "images"
CROPS_DIR = IMAGES_DIR / "crops"
FLAGS_DIR = IMAGES_DIR / "flags"

# Colors for stat arrows
STAT_UP_COLOR = (0, 200, 83)     # Green
STAT_DOWN_COLOR = (220, 53, 69)  # Red
STAT_FLAT_COLOR = (160, 170, 185)


@dataclass
class SlideContent:
    """Content for a single video slide."""

    slide_type: str  # "title", "section", "closing"
    headline: str = ""
    bullets: list[str] = field(default_factory=list)
    subtitle: str = ""
    week_label: str = ""


class VideoAssembler:
    """
    Assembles Intelligence Signal video from slides and audio.

    Creates PIL-rendered slides and combines them with audio narration
    via ffmpeg into a final MP4 video.
    """

    def __init__(self):
        self._font_regular = None
        self._font_bold = None
        self._font_loaded = False

    def _load_fonts(self):
        """Load Inter fonts with graceful fallback to PIL default."""
        if self._font_loaded:
            return

        try:
            from PIL import ImageFont

            if FONT_BOLD.exists():
                self._font_bold = {
                    "title": ImageFont.truetype(str(FONT_BOLD), 72),
                    "headline": ImageFont.truetype(str(FONT_BOLD), 48),
                    "label": ImageFont.truetype(str(FONT_BOLD), 28),
                }
            if FONT_REGULAR.exists():
                self._font_regular = {
                    "body": ImageFont.truetype(str(FONT_REGULAR), 36),
                    "subtitle": ImageFont.truetype(str(FONT_REGULAR), 32),
                    "small": ImageFont.truetype(str(FONT_REGULAR), 24),
                }

            if self._font_bold and self._font_regular:
                logger.info("Inter fonts loaded for video slides")
            else:
                logger.warning(
                    "Inter fonts not found, using PIL defaults. "
                    "Download to assets/fonts/ for better rendering."
                )
                self._font_bold = None
                self._font_regular = None

        except Exception as e:
            logger.warning(f"Font loading failed, using defaults: {e}")
            self._font_bold = None
            self._font_regular = None

        self._font_loaded = True

    def _get_font(self, style: str):
        """Get a font by style name, with PIL default fallback."""
        self._load_fonts()

        from PIL import ImageFont

        if self._font_bold and style in self._font_bold:
            return self._font_bold[style]
        if self._font_regular and style in self._font_regular:
            return self._font_regular[style]

        # Fallback: PIL default
        size_map = {
            "title": 72,
            "headline": 48,
            "body": 36,
            "subtitle": 32,
            "label": 28,
            "small": 24,
        }
        try:
            return ImageFont.truetype("arial.ttf", size_map.get(style, 32))
        except OSError:
            return ImageFont.load_default()

    def create_slides(self, sections: list[dict]) -> list[bytes]:
        """
        Create slide images from script sections.

        Args:
            sections: List of dicts with:
                - type: "title" | "section" | "closing"
                - headline: str
                - bullets: list[str] (for section slides)
                - subtitle: str (for title/closing slides)
                - week_label: str (for title slide)

        Returns:
            List of PNG image bytes.
        """
        from PIL import Image, ImageDraw

        slides = []

        for section in sections:
            img = Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), BG_COLOR)
            draw = ImageDraw.Draw(img)

            slide_type = section.get("type", "section")

            if slide_type == "title":
                self._draw_title_slide(draw, section)
            elif slide_type == "closing":
                self._draw_closing_slide(draw, section)
            else:
                self._draw_section_slide(draw, section)

            # Add accent bar at bottom
            draw.rectangle(
                [(0, SLIDE_HEIGHT - 6), (SLIDE_WIDTH, SLIDE_HEIGHT)],
                fill=ACCENT_COLOR,
            )

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            slides.append(buf.getvalue())

        logger.info(f"Created {len(slides)} video slides")
        return slides

    def _draw_title_slide(self, draw, section: dict):
        """Render the title slide."""
        # Week label
        week_label = section.get("week_label", "")
        if week_label:
            draw.text(
                (120, 280),
                week_label,
                fill=ACCENT_COLOR,
                font=self._get_font("label"),
            )

        # Main title
        draw.text(
            (120, 340),
            "CropSight",
            fill=TEXT_COLOR,
            font=self._get_font("title"),
        )
        draw.text(
            (120, 430),
            "Intelligence Signal",
            fill=ACCENT_COLOR,
            font=self._get_font("title"),
        )

        # Subtitle
        subtitle = section.get("subtitle", "")
        if subtitle:
            draw.text(
                (120, 560),
                subtitle,
                fill=MUTED_COLOR,
                font=self._get_font("subtitle"),
            )

        # Accent line
        draw.rectangle(
            [(120, 530), (600, 534)],
            fill=ACCENT_COLOR,
        )

    def _draw_section_slide(self, draw, section: dict):
        """Render a content section slide."""
        # Headline
        headline = section.get("headline", "")
        if headline:
            draw.text(
                (120, 120),
                headline,
                fill=ACCENT_COLOR,
                font=self._get_font("headline"),
            )

            # Accent line under headline
            draw.rectangle(
                [(120, 185), (120 + min(len(headline) * 20, 800), 189)],
                fill=ACCENT_COLOR,
            )

        # Bullets
        bullets = section.get("bullets", [])
        y_offset = 240
        for bullet in bullets[:5]:  # Max 5 bullets per slide
            # Bullet dot
            draw.ellipse(
                [(130, y_offset + 12), (142, y_offset + 24)],
                fill=ACCENT_COLOR,
            )
            # Wrap long text
            lines = self._wrap_text(bullet, max_chars=70)
            for line in lines:
                draw.text(
                    (165, y_offset),
                    line,
                    fill=TEXT_COLOR,
                    font=self._get_font("body"),
                )
                y_offset += 50
            y_offset += 20  # Extra space between bullets

    def _draw_closing_slide(self, draw, section: dict):
        """Render the closing slide."""
        draw.text(
            (120, 340),
            "CropSight Intelligence Signal",
            fill=TEXT_COLOR,
            font=self._get_font("headline"),
        )

        subtitle = section.get("subtitle", "Read the full report.")
        draw.text(
            (120, 420),
            subtitle,
            fill=MUTED_COLOR,
            font=self._get_font("subtitle"),
        )

        # CropSight green accent bar
        draw.rectangle(
            [(120, 500), (700, 504)],
            fill=ACCENT_COLOR,
        )

        draw.text(
            (120, 540),
            "cropsight.com",
            fill=ACCENT_COLOR,
            font=self._get_font("small"),
        )

    def _wrap_text(self, text: str, max_chars: int = 70) -> list[str]:
        """Wrap text to fit slide width."""
        words = text.split()
        lines = []
        current_line = ""

        for word in words:
            if len(current_line) + len(word) + 1 <= max_chars:
                current_line = f"{current_line} {word}".strip()
            else:
                if current_line:
                    lines.append(current_line)
                current_line = word

        if current_line:
            lines.append(current_line)

        return lines or [""]

    async def assemble_video(
        self,
        slide_images: list[bytes],
        audio_bytes: bytes,
        fps: int = 1,
    ) -> Optional[bytes]:
        """
        Assemble slides and audio into an MP4 video via ffmpeg.

        Each slide is shown for audio_duration / num_slides seconds.

        Args:
            slide_images: List of PNG image bytes.
            audio_bytes: MP3 audio narration.
            fps: Frames per second (1 = static slides).

        Returns:
            MP4 video bytes, or None on failure.
        """
        if not slide_images or not audio_bytes:
            logger.warning("Cannot assemble video: missing slides or audio")
            return None

        tmp_dir = tempfile.mkdtemp(prefix="gianluigi_video_")

        try:
            # Write audio file
            audio_path = os.path.join(tmp_dir, "narration.mp3")
            with open(audio_path, "wb") as f:
                f.write(audio_bytes)

            # Get audio duration using ffprobe
            duration = await self._get_audio_duration(audio_path)
            if not duration or duration <= 0:
                duration = len(slide_images) * 8  # Fallback: 8s per slide

            slide_duration = duration / len(slide_images)

            # Write slide images as numbered PNGs
            for i, img_bytes in enumerate(slide_images):
                slide_path = os.path.join(tmp_dir, f"slide_{i:03d}.png")
                with open(slide_path, "wb") as f:
                    f.write(img_bytes)

            # Build ffmpeg concat file
            concat_path = os.path.join(tmp_dir, "concat.txt")
            with open(concat_path, "w") as f:
                for i in range(len(slide_images)):
                    f.write(f"file 'slide_{i:03d}.png'\n")
                    f.write(f"duration {slide_duration:.2f}\n")
                # Repeat last frame (ffmpeg concat quirk)
                f.write(f"file 'slide_{len(slide_images) - 1:03d}.png'\n")

            # Assemble video
            output_path = os.path.join(tmp_dir, "output.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", concat_path,
                "-i", audio_path,
                "-c:v", "libx264",
                "-profile:v", "baseline",
                "-level", "3.1",
                "-pix_fmt", "yuv420p",
                "-preset", "medium",
                "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                "-movflags", "+faststart",
                output_path,
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=120
            )

            if process.returncode != 0:
                logger.error(f"ffmpeg failed: {stderr.decode()[:500]}")
                return None

            with open(output_path, "rb") as f:
                video_bytes = f.read()

            logger.info(
                f"Video assembled: {len(slide_images)} slides, "
                f"{duration:.1f}s, {len(video_bytes)} bytes"
            )
            return video_bytes

        except asyncio.TimeoutError:
            logger.error("ffmpeg timed out after 120s")
            return None
        except FileNotFoundError:
            logger.error(
                "ffmpeg not found. Install ffmpeg or disable video generation."
            )
            return None
        except Exception as e:
            logger.error(f"Video assembly failed: {e}")
            return None
        finally:
            # Cleanup temp files
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass

    async def _get_audio_duration(self, audio_path: str) -> Optional[float]:
        """Get audio duration in seconds using ffprobe."""
        try:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=10
            )
            return float(stdout.decode().strip())
        except Exception as e:
            logger.warning(f"Could not get audio duration: {e}")
            return None

    def parse_script_to_sections(self, script_text: str) -> list[dict]:
        """
        Parse a narration script into slide sections.

        Splits the script into 6-8 segments for slide rendering.
        Creates title, content, and closing slides.

        Args:
            script_text: The narration script text.

        Returns:
            List of section dicts for create_slides().
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        week = now.isocalendar()[1]
        year = now.isocalendar()[0]

        # Split script into sentences
        sentences = [
            s.strip() for s in script_text.replace("\n", " ").split(".")
            if s.strip()
        ]

        sections = []

        # Title slide
        sections.append({
            "type": "title",
            "headline": "CropSight Intelligence Signal",
            "week_label": f"WEEK {week} / {year}",
            "subtitle": sentences[0] + "." if sentences else "",
        })

        # Content slides — group sentences into slides of 2-3 each
        content_sentences = sentences[1:-1] if len(sentences) > 2 else sentences[1:]
        chunk_size = max(2, len(content_sentences) // 5)  # Target ~5 content slides

        for i in range(0, len(content_sentences), chunk_size):
            chunk = content_sentences[i:i + chunk_size]
            if chunk:
                sections.append({
                    "type": "section",
                    "headline": f"Signal {len(sections)}",
                    "bullets": [s + "." for s in chunk],
                })

        # Closing slide
        closing_text = sentences[-1] + "." if sentences else "Read the full report."
        sections.append({
            "type": "closing",
            "subtitle": closing_text,
        })

        # Cap at 8 slides
        if len(sections) > 8:
            sections = sections[:7] + [sections[-1]]

        return sections

    # ── Enhanced pipeline (per-segment) ────────────────────────────────

    async def assemble_video_segments(
        self,
        segments: list[dict],
        audio_clips: list[bytes],
    ) -> Optional[bytes]:
        """
        Assemble video from structured segments with per-segment audio.

        Each segment has its own audio clip, slide image, and timing.
        This produces much better sync than the old equal-time approach.

        Args:
            segments: List of structured segment dicts from LLM.
            audio_clips: Corresponding MP3 audio bytes per segment.

        Returns:
            MP4 video bytes, or None on failure.
        """
        if not segments or not audio_clips:
            logger.warning("Cannot assemble: missing segments or audio")
            return None

        if len(segments) != len(audio_clips):
            logger.warning(
                f"Segment/audio mismatch: {len(segments)} segments, "
                f"{len(audio_clips)} clips"
            )
            return None

        tmp_dir = tempfile.mkdtemp(prefix="gianluigi_video_")
        compositor = SlideCompositor(self)

        try:
            # Write audio clips and get durations
            durations = []
            for i, clip in enumerate(audio_clips):
                audio_path = os.path.join(tmp_dir, f"audio_{i:03d}.mp3")
                with open(audio_path, "wb") as f:
                    f.write(clip)
                dur = await self._get_audio_duration(audio_path)
                durations.append(dur or 5.0)  # fallback 5s

            # Render slides
            total_segments = len(segments)
            for i, segment in enumerate(segments):
                segment["_index"] = i + 1
                segment["_total"] = total_segments
                slide_png = compositor.render_segment(segment)
                slide_path = os.path.join(tmp_dir, f"slide_{i:03d}.png")
                with open(slide_path, "wb") as f:
                    f.write(slide_png)

            # Concatenate audio clips into single file
            audio_list_path = os.path.join(tmp_dir, "audio_list.txt")
            with open(audio_list_path, "w") as f:
                for i in range(len(audio_clips)):
                    f.write(f"file 'audio_{i:03d}.mp3'\n")

            combined_audio_path = os.path.join(tmp_dir, "combined.mp3")
            concat_cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", audio_list_path,
                "-c", "copy",
                combined_audio_path,
            ]
            proc = await asyncio.create_subprocess_exec(
                *concat_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)

            # Build video concat file with per-segment durations
            concat_path = os.path.join(tmp_dir, "video_concat.txt")
            with open(concat_path, "w") as f:
                for i, dur in enumerate(durations):
                    f.write(f"file 'slide_{i:03d}.png'\n")
                    f.write(f"duration {dur:.2f}\n")
                # Repeat last frame (ffmpeg concat quirk)
                f.write(f"file 'slide_{len(durations) - 1:03d}.png'\n")

            # Assemble final video
            output_path = os.path.join(tmp_dir, "output.mp4")
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0", "-i", concat_path,
                "-i", combined_audio_path,
                "-c:v", "libx264",
                "-profile:v", "baseline",
                "-level", "3.1",
                "-pix_fmt", "yuv420p",
                "-preset", "medium",
                "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                "-movflags", "+faststart",
                output_path,
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=120
            )

            if process.returncode != 0:
                logger.error(f"ffmpeg segments failed: {stderr.decode()[:500]}")
                return None

            with open(output_path, "rb") as f:
                video_bytes = f.read()

            total_dur = sum(durations)
            logger.info(
                f"Segment video assembled: {len(segments)} segments, "
                f"{total_dur:.1f}s, {len(video_bytes) / 1024 / 1024:.1f} MB"
            )
            return video_bytes

        except asyncio.TimeoutError:
            logger.error("ffmpeg segment assembly timed out")
            return None
        except FileNotFoundError:
            logger.error("ffmpeg not found")
            return None
        except Exception as e:
            logger.error(f"Segment video assembly failed: {e}")
            return None
        finally:
            try:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass


class SlideCompositor:
    """
    Renders enhanced video slides based on structured segment metadata.

    Each segment type gets a different visual treatment:
    - headline: CropSight branding + title
    - stat: chart/indicator with data visualization
    - crop: crop photo background with lower-third text
    - country: flag + text overlay
    - competitor: news-style text card
    - closing: CropSight branding + CTA
    """

    def __init__(self, assembler: VideoAssembler):
        self.assembler = assembler

    def render_segment(self, segment: dict) -> bytes:
        """Render a segment into a PNG image based on its type."""
        segment_type = segment.get("segment_type", "headline")
        narration = segment.get("narration", "")

        renderer = {
            "headline": self._render_headline,
            "stat": self._render_stat,
            "crop": self._render_crop,
            "country": self._render_country,
            "competitor": self._render_competitor,
            "closing": self._render_closing,
        }.get(segment_type, self._render_default)

        from PIL import Image

        img = renderer(segment)
        self._add_overlays(img, segment)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _render_headline(self, segment: dict):
        """Title card with CropSight branding."""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # Week label
        draw.text(
            (120, 300),
            "CROPSIGHT INTELLIGENCE SIGNAL",
            fill=ACCENT_COLOR,
            font=self.assembler._get_font("label"),
        )

        # Narration as title text (first sentence)
        narration = segment.get("narration", "")
        lines = self.assembler._wrap_text(narration, max_chars=50)
        y = 370
        for line in lines[:3]:
            draw.text(
                (120, y), line, fill=TEXT_COLOR,
                font=self.assembler._get_font("headline"),
            )
            y += 65

        # Accent bar
        draw.rectangle([(120, 350), (500, 354)], fill=ACCENT_COLOR)

        return img

    def _render_stat(self, segment: dict):
        """Statistics slide with chart indicator."""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        data = segment.get("data", {})
        label = data.get("label", "")
        value = data.get("value", "")
        direction = data.get("direction", "up")

        # Render chart indicator with matplotlib
        chart_img = self._render_stat_chart(label, value, direction)
        if chart_img:
            # Paste chart on left side
            img.paste(chart_img, (80, 150), chart_img if chart_img.mode == "RGBA" else None)

        # Narration text on right side
        narration = segment.get("narration", "")
        lines = self.assembler._wrap_text(narration, max_chars=40)
        y = 200
        for line in lines:
            draw.text(
                (1000, y), line, fill=TEXT_COLOR,
                font=self.assembler._get_font("body"),
            )
            y += 50

        return img

    def _render_stat_chart(
        self, label: str, value: str, direction: str
    ) -> Optional["Image.Image"]:
        """Render a stat indicator using matplotlib."""
        try:
            import matplotlib.pyplot as plt
            from PIL import Image

            fig, ax = plt.subplots(figsize=(8, 6))
            fig.patch.set_alpha(0)
            ax.set_facecolor((0, 0, 0, 0))

            # Arrow direction
            arrow_color = (
                "#00C853" if direction == "up"
                else "#DC3545" if direction == "down"
                else "#A0AAB9"
            )
            arrow_char = "\u25B2" if direction == "up" else "\u25BC" if direction == "down" else "\u25C6"

            # Large value display
            ax.text(
                0.5, 0.6, f"{arrow_char} {value}",
                fontsize=72, fontweight="bold",
                color=arrow_color, ha="center", va="center",
                transform=ax.transAxes,
            )

            # Label below
            ax.text(
                0.5, 0.25, label,
                fontsize=28, color="white", ha="center", va="center",
                transform=ax.transAxes,
            )

            ax.axis("off")

            buf = io.BytesIO()
            fig.savefig(buf, format="png", transparent=True, dpi=100, bbox_inches="tight")
            plt.close(fig)

            buf.seek(0)
            chart = Image.open(buf).convert("RGBA")

            # Resize to fit slide
            chart = chart.resize((800, 600), Image.LANCZOS)
            return chart

        except Exception as e:
            logger.warning(f"Chart render failed: {e}")
            return None

    def _render_crop(self, segment: dict):
        """Crop photo background with dark overlay and text."""
        from PIL import Image, ImageDraw

        crop_name = segment.get("crop", "wheat")
        img = self._load_background_image(crop_name)
        draw = ImageDraw.Draw(img)

        # Dark overlay for text readability
        overlay = Image.new("RGBA", (SLIDE_WIDTH, SLIDE_HEIGHT), (10, 22, 40, 160))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

        # Crop name label
        draw.text(
            (120, 200),
            crop_name.upper(),
            fill=ACCENT_COLOR,
            font=self.assembler._get_font("label"),
        )

        # Narration text
        narration = segment.get("narration", "")
        lines = self.assembler._wrap_text(narration, max_chars=55)
        y = 280
        for line in lines:
            draw.text(
                (120, y), line, fill=TEXT_COLOR,
                font=self.assembler._get_font("headline"),
            )
            y += 65

        return img

    def _render_country(self, segment: dict):
        """Country flag with text overlay."""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        country_code = segment.get("country_code", "")

        # Load flag if available
        flag_img = self._load_flag(country_code)
        if flag_img:
            # Place flag on the left
            flag_resized = flag_img.resize((300, 200), Image.LANCZOS)
            img.paste(flag_resized, (120, 250))

        # Country code label
        draw.text(
            (120, 180),
            country_code.upper() if country_code else "REGIONAL",
            fill=ACCENT_COLOR,
            font=self.assembler._get_font("label"),
        )

        # Narration text
        narration = segment.get("narration", "")
        lines = self.assembler._wrap_text(narration, max_chars=45)
        y = 250 if not flag_img else 500
        x = 500 if flag_img else 120
        for line in lines:
            draw.text(
                (x, y), line, fill=TEXT_COLOR,
                font=self.assembler._get_font("body"),
            )
            y += 50

        return img

    def _render_competitor(self, segment: dict):
        """Competitor news text card."""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        entity = segment.get("entity", "")

        # Category label
        draw.text(
            (120, 200),
            "COMPETITIVE LANDSCAPE",
            fill=ACCENT_COLOR,
            font=self.assembler._get_font("label"),
        )

        # Entity name (large)
        if entity:
            draw.text(
                (120, 260),
                entity,
                fill=TEXT_COLOR,
                font=self.assembler._get_font("headline"),
            )

        # Accent bar
        draw.rectangle([(120, 330), (600, 334)], fill=ACCENT_COLOR)

        # Narration text
        narration = segment.get("narration", "")
        lines = self.assembler._wrap_text(narration, max_chars=55)
        y = 370
        for line in lines:
            draw.text(
                (120, y), line, fill=TEXT_COLOR,
                font=self.assembler._get_font("body"),
            )
            y += 50

        return img

    def _render_closing(self, segment: dict):
        """Closing slide with CropSight branding."""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        draw.text(
            (120, 350),
            "CropSight Intelligence Signal",
            fill=TEXT_COLOR,
            font=self.assembler._get_font("headline"),
        )

        draw.text(
            (120, 430),
            "Read the full report.",
            fill=MUTED_COLOR,
            font=self.assembler._get_font("subtitle"),
        )

        draw.rectangle([(120, 500), (700, 504)], fill=ACCENT_COLOR)

        draw.text(
            (120, 540),
            "cropsight.com",
            fill=ACCENT_COLOR,
            font=self.assembler._get_font("small"),
        )

        return img

    def _render_default(self, segment: dict):
        """Default render for unknown segment types."""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        narration = segment.get("narration", "")
        lines = self.assembler._wrap_text(narration, max_chars=55)
        y = 300
        for line in lines:
            draw.text(
                (120, y), line, fill=TEXT_COLOR,
                font=self.assembler._get_font("body"),
            )
            y += 50

        return img

    def _add_overlays(self, img, segment: dict):
        """Add watermark, progress indicator, and source citation."""
        from PIL import ImageDraw

        draw = ImageDraw.Draw(img)

        # CropSight watermark (top-right)
        draw.text(
            (SLIDE_WIDTH - 250, 30),
            "CROPSIGHT",
            fill=(*MUTED_COLOR, 100) if img.mode == "RGBA" else MUTED_COLOR,
            font=self.assembler._get_font("small"),
        )

        # Progress indicator (bottom-right)
        idx = segment.get("_index", 0)
        total = segment.get("_total", 0)
        if idx and total:
            draw.text(
                (SLIDE_WIDTH - 120, SLIDE_HEIGHT - 60),
                f"{idx}/{total}",
                fill=MUTED_COLOR,
                font=self.assembler._get_font("small"),
            )

        # Bottom accent bar
        draw.rectangle(
            [(0, SLIDE_HEIGHT - 6), (SLIDE_WIDTH, SLIDE_HEIGHT)],
            fill=ACCENT_COLOR,
        )

        # Source citation for stat/country slides
        seg_type = segment.get("segment_type", "")
        if seg_type in ("stat", "country"):
            draw.text(
                (120, SLIDE_HEIGHT - 50),
                "Source: Perplexity Research",
                fill=MUTED_COLOR,
                font=self.assembler._get_font("small"),
            )

    def _load_background_image(self, crop_name: str):
        """Load a crop background image, fallback to solid color."""
        from PIL import Image

        # Try to find matching crop image
        for ext in (".jpg", ".jpeg", ".png"):
            path = CROPS_DIR / f"{crop_name.lower()}{ext}"
            if path.exists():
                try:
                    img = Image.open(path).convert("RGB")
                    return img.resize((SLIDE_WIDTH, SLIDE_HEIGHT), Image.LANCZOS)
                except Exception:
                    pass

        # Fallback: solid color
        return Image.new("RGB", (SLIDE_WIDTH, SLIDE_HEIGHT), BG_COLOR)

    def _load_flag(self, country_code: str):
        """Load a country flag image, return None if not found."""
        from PIL import Image

        if not country_code:
            return None

        for ext in (".png", ".jpg", ".svg"):
            path = FLAGS_DIR / f"{country_code.upper()}{ext}"
            if path.exists():
                try:
                    return Image.open(path).convert("RGBA")
                except Exception:
                    pass

        return None


# Singleton
video_assembler = VideoAssembler()
