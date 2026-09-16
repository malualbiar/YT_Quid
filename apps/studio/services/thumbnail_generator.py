import os
import math
import logging
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance
from django.conf import settings

logger = logging.getLogger(__name__)

CANVAS_WIDTH = 1280
CANVAS_HEIGHT = 720


class ThumbnailGeneratorService:
    """
    Automated YouTube Thumbnail Generator for Lyric Videos.
    Produces high-impact, professional 1280x720 HD thumbnails in 3 styles:
      1. FROSTED_SCRIM (Default): Dark frosted gradient scrim to mask existing text/watermarks.
      2. AMBIENT_CARD: Ambient blurred canvas with a sharp framed square artwork card on the left.
      3. THEMED_BACKDROP: Procedural aesthetic gradient backdrop with centerpiece typography.
    """

    class Style:
        FROSTED_SCRIM = 'FROSTED_SCRIM'
        AMBIENT_CARD = 'AMBIENT_CARD'
        THEMED_BACKDROP = 'THEMED_BACKDROP'

    THEME_PALETTES = {
        'DARK_STUDIO': {
            'bg_start': (12, 14, 20),
            'bg_end': (28, 32, 45),
            'accent': (0, 229, 255),       # Cyan
            'accent_secondary': (168, 85, 247), # Purple
            'badge_bg': (0, 229, 255, 45),
            'badge_border': (0, 229, 255, 180),
            'badge_text': (200, 250, 255),
        },
        'CYBERPUNK_NEON': {
            'bg_start': (15, 8, 28),
            'bg_end': (40, 10, 50),
            'accent': (236, 72, 153),      # Pink/Magenta
            'accent_secondary': (0, 229, 255), # Cyan
            'badge_bg': (236, 72, 153, 45),
            'badge_border': (236, 72, 153, 180),
            'badge_text': (255, 215, 240),
        },
        'LOFI_SUNSET': {
            'bg_start': (24, 14, 20),
            'bg_end': (55, 25, 25),
            'accent': (245, 158, 11),      # Amber/Gold
            'accent_secondary': (244, 63, 94), # Rose
            'badge_bg': (245, 158, 11, 45),
            'badge_border': (245, 158, 11, 180),
            'badge_text': (255, 240, 200),
        },
        'ROYAL_GOLD': {
            'bg_start': (10, 10, 14),
            'bg_end': (35, 30, 15),
            'accent': (234, 179, 8),       # Gold
            'accent_secondary': (217, 119, 6),
            'badge_bg': (234, 179, 8, 45),
            'badge_border': (234, 179, 8, 180),
            'badge_text': (255, 245, 210),
        },
        'DEEP_COSMIC': {
            'bg_start': (8, 10, 24),
            'bg_end': (20, 25, 55),
            'accent': (99, 102, 241),      # Indigo
            'accent_secondary': (168, 85, 247),
            'badge_bg': (99, 102, 241, 45),
            'badge_border': (99, 102, 241, 180),
            'badge_text': (225, 230, 255),
        },
    }

    @classmethod
    def generate_thumbnail(
        cls,
        title: str,
        artist: str = '',
        background_image_path: str = None,
        style: str = 'FROSTED_SCRIM',
        badge_text: str = 'OFFICIAL LYRIC VIDEO',
        theme_palette: str = 'DARK_STUDIO',
        output_path: str = None,
        project_id: int = None,
    ) -> str:
        """
        Main generator entrypoint. Returns the absolute path of the generated JPEG thumbnail.
        """
        style = (style or cls.Style.FROSTED_SCRIM).upper()
        if style not in [cls.Style.FROSTED_SCRIM, cls.Style.AMBIENT_CARD, cls.Style.THEMED_BACKDROP]:
            style = cls.Style.FROSTED_SCRIM

        palette = cls.THEME_PALETTES.get(theme_palette, cls.THEME_PALETTES['DARK_STUDIO'])

        # Prepare output destination
        if not output_path:
            thumb_dir = os.path.join(settings.MEDIA_ROOT, 'studio', 'thumbnails')
            os.makedirs(thumb_dir, exist_ok=True)
            proj_tag = f"proj_{project_id}_" if project_id else ""
            clean_title = "".join(c for c in (title or 'thumb') if c.isalnum() or c in ' _-').strip().replace(' ', '_')[:25]
            output_path = os.path.join(thumb_dir, f"auto_thumb_{proj_tag}{clean_title}.jpg")
        else:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

        try:
            if style == cls.Style.AMBIENT_CARD:
                img = cls._render_ambient_card(title, artist, background_image_path, badge_text, palette)
            elif style == cls.Style.THEMED_BACKDROP:
                img = cls._render_themed_backdrop(title, artist, badge_text, palette)
            else:
                img = cls._render_frosted_scrim(title, artist, background_image_path, badge_text, palette)

            # Save final image as RGB JPEG with high quality
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(output_path, 'JPEG', quality=95, optimize=True)
            logger.info(f"Generated thumbnail successfully: {output_path}")
            return output_path

        except Exception as e:
            logger.error(f"Thumbnail generation error: {e}", exc_info=True)
            # Fallback to minimal emergency thumbnail
            fallback_img = cls._render_fallback_card(title, artist, badge_text, palette)
            if fallback_img.mode != 'RGB':
                fallback_img = fallback_img.convert('RGB')
            fallback_img.save(output_path, 'JPEG', quality=90)
            return output_path

    # -------------------------------------------------------------------------
    # Style 1: Frosted Dark Scrim (Masks Old Text & Watermarks) - DEFAULT
    # -------------------------------------------------------------------------
    @classmethod
    def _render_frosted_scrim(cls, title, artist, bg_path, badge_text, palette):
        # 1. Base image or procedural background
        if bg_path and os.path.exists(bg_path):
            base = cls._load_and_cover_fill(bg_path, CANVAS_WIDTH, CANVAS_HEIGHT)
            # Boost vibrancy slightly
            enhancer = ImageEnhance.Color(base)
            base = enhancer.enhance(1.15)
            enhancer = ImageEnhance.Contrast(base)
            base = enhancer.enhance(1.08)
        else:
            base = cls._generate_mesh_gradient(CANVAS_WIDTH, CANVAS_HEIGHT, palette)

        # 2. Apply dark frosted gradient scrim over bottom and left
        # This completely obscures any text/watermarks in the lower 65% of the original thumbnail
        scrim = Image.new('RGBA', (CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0, 0, 0))
        scrim_draw = ImageDraw.Draw(scrim)

        # Vertical bottom-up dark gradient
        for y in range(CANVAS_HEIGHT):
            progress = y / CANVAS_HEIGHT
            if progress < 0.25:
                alpha = int(progress * 100)
            elif progress < 0.55:
                p2 = (progress - 0.25) / 0.30
                alpha = int(25 + p2 * 140)
            else:
                p3 = (progress - 0.55) / 0.45
                alpha = int(165 + p3 * 85) # Up to 250 (almost opaque solid at bottom)

            scrim_draw.line([(0, y), (CANVAS_WIDTH, y)], fill=(8, 10, 15, alpha))

        # Radial / Left-side darkening for left-aligned text readability
        left_scrim = Image.new('RGBA', (CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0, 0, 0))
        left_draw = ImageDraw.Draw(left_scrim)
        for x in range(int(CANVAS_WIDTH * 0.75)):
            factor = 1.0 - (x / (CANVAS_WIDTH * 0.75))
            alpha = int(factor * factor * 160)
            left_draw.line([(x, 0), (x, CANVAS_HEIGHT)], fill=(8, 10, 15, alpha))

        base = Image.alpha_composite(base.convert('RGBA'), scrim)
        base = Image.alpha_composite(base, left_scrim)

        draw = ImageDraw.Draw(base)

        # 4. Draw Pill Badge at top-left
        if badge_text:
            cls._draw_pill_badge(draw, x=60, y=55, text=badge_text, palette=palette)

        # 5. Draw Artist & Song Title
        cls._draw_title_and_artist_left(
            draw=draw,
            base_image=base,
            title=title or 'Untitled Track',
            artist=artist,
            max_width=950,
            x=60,
            bottom_y=CANVAS_HEIGHT - 65,
            palette=palette
        )

        return base

    # -------------------------------------------------------------------------
    # Style 2: Ambient Blurred Canvas + Framed Artwork Card
    # -------------------------------------------------------------------------
    @classmethod
    def _render_ambient_card(cls, title, artist, bg_path, badge_text, palette):
        # 1. Background: Heavily blurred version of source image
        if bg_path and os.path.exists(bg_path):
            src_cover = Image.open(bg_path).convert('RGB')
            bg_base = cls._load_and_cover_fill(bg_path, CANVAS_WIDTH, CANVAS_HEIGHT)
            bg_blurred = bg_base.filter(ImageFilter.GaussianBlur(radius=38))
            # Darken blurred canvas
            enhancer = ImageEnhance.Brightness(bg_blurred)
            bg_blurred = enhancer.enhance(0.42)
        else:
            src_cover = None
            bg_blurred = cls._generate_mesh_gradient(CANVAS_WIDTH, CANVAS_HEIGHT, palette)

        base = bg_blurred.convert('RGBA')
        draw = ImageDraw.Draw(base)

        # 2. Framed Artwork Card on the Left
        card_size = 480
        card_x = 75
        card_y = int((CANVAS_HEIGHT - card_size) / 2)

        if src_cover:
            # Crop square
            w, h = src_cover.size
            min_dim = min(w, h)
            left = (w - min_dim) // 2
            top = (h - min_dim) // 2
            square_cover = src_cover.crop((left, top, left + min_dim, top + min_dim))
            square_cover = square_cover.resize((card_size, card_size), Image.Resampling.LANCZOS).convert('RGBA')

            # Draw card drop shadow
            shadow = Image.new('RGBA', (CANVAS_WIDTH, CANVAS_HEIGHT), (0, 0, 0, 0))
            s_draw = ImageDraw.Draw(shadow)
            s_draw.rounded_rectangle(
                [card_x + 10, card_y + 12, card_x + card_size + 10, card_y + card_size + 12],
                radius=18,
                fill=(0, 0, 0, 180)
            )
            shadow = shadow.filter(ImageFilter.GaussianBlur(radius=16))
            base = Image.alpha_composite(base, shadow)

            # Mask with rounded corners
            mask = Image.new('L', (card_size, card_size), 0)
            m_draw = ImageDraw.Draw(mask)
            m_draw.rounded_rectangle([0, 0, card_size, card_size], radius=18, fill=255)
            base.paste(square_cover, (card_x, card_y), mask)

            # Draw sleek card border
            draw = ImageDraw.Draw(base)
            draw.rounded_rectangle(
                [card_x, card_y, card_x + card_size, card_y + card_size],
                radius=18,
                outline=(255, 255, 255, 50),
                width=2
            )
        else:
            # Placeholder vinyl disc / art card
            cls._draw_procedural_vinyl_card(base, card_x, card_y, card_size, palette)
            draw = ImageDraw.Draw(base)

        # 3. Typography on the Right
        text_x = card_x + card_size + 55
        max_text_w = CANVAS_WIDTH - text_x - 60

        if badge_text:
            cls._draw_pill_badge(draw, x=text_x, y=card_y + 15, text=badge_text, palette=palette)

        cls._draw_title_and_artist_stacked(
            draw=draw,
            base_image=base,
            title=title or 'Untitled Track',
            artist=artist,
            x=text_x,
            y=card_y + 75,
            max_width=max_text_w,
            max_height=card_size - 85,
            palette=palette
        )

        return base

    # -------------------------------------------------------------------------
    # Style 3: Themed Aesthetic Backdrop (Procedural Cinematic)
    # -------------------------------------------------------------------------
    @classmethod
    def _render_themed_backdrop(cls, title, artist, badge_text, palette):
        base = cls._generate_mesh_gradient(CANVAS_WIDTH, CANVAS_HEIGHT, palette)
        draw = ImageDraw.Draw(base)

        # Decorative audio spectrum / waveforms
        cls._draw_decorative_soundwave(draw, CANVAS_WIDTH, CANVAS_HEIGHT, palette)

        # Draw Center Badge
        if badge_text:
            cls._draw_pill_badge(draw, x=int(CANVAS_WIDTH / 2) - 120, y=110, text=badge_text, palette=palette, centered=True)

        # Draw Big Centerpiece Typography
        cls._draw_title_and_artist_centered(
            draw=draw,
            base_image=base,
            title=title or 'Untitled Track',
            artist=artist,
            max_width=1100,
            center_y=int(CANVAS_HEIGHT / 2) + 30,
            palette=palette
        )

        return base

    # -------------------------------------------------------------------------
    # Fallback Card
    # -------------------------------------------------------------------------
    @classmethod
    def _render_fallback_card(cls, title, artist, badge_text, palette):
        base = Image.new('RGB', (CANVAS_WIDTH, CANVAS_HEIGHT), (15, 17, 26))
        draw = ImageDraw.Draw(base)
        cls._draw_title_and_artist_centered(
            draw=draw,
            base_image=base,
            title=title or 'Untitled Track',
            artist=artist,
            max_width=1100,
            center_y=int(CANVAS_HEIGHT / 2),
            palette=palette
        )
        return base

    # -------------------------------------------------------------------------
    # Typography Helpers
    # -------------------------------------------------------------------------
    @classmethod
    def _get_font(cls, font_name='Arial', size=48, bold=True):
        """Locates best TTF font with fallback to system fonts."""
        candidates = []
        if bold:
            candidates.extend([
                'C:\\Windows\\Fonts\\arialbd.ttf',
                'C:\\Windows\\Fonts\\impact.ttf',
                'C:\\Windows\\Fonts\\segoeuib.ttf',
                'C:\\Windows\\Fonts\\trebucbd.ttf',
                'C:\\Windows\\Fonts\\tahomabd.ttf',
            ])
        candidates.extend([
            'C:\\Windows\\Fonts\\arial.ttf',
            'C:\\Windows\\Fonts\\segoeui.ttf',
            'C:\\Windows\\Fonts\\tahoma.ttf',
            os.path.join(settings.BASE_DIR, 'static', 'fonts', 'RubikIso.ttf'),
            os.path.join(settings.BASE_DIR, 'static', 'fonts', 'DynaPuff.ttf'),
        ])

        for path in candidates:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size=int(size))
                except Exception:
                    continue

        # Fallback
        try:
            return ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    @classmethod
    def _draw_pill_badge(cls, draw, x, y, text, palette, centered=False):
        font = cls._get_font('Arial', size=20, bold=True)
        text_bbox = draw.textbbox((0, 0), text, font=font)
        tw = text_bbox[2] - text_bbox[0]
        th = text_bbox[3] - text_bbox[1]

        pad_x, pad_y = 16, 7
        bw = tw + pad_x * 2 + 18 # +18 for glow dot
        bh = th + pad_y * 2

        if centered:
            bx = x - (bw // 2)
        else:
            bx = x
        by = y

        # Badge background
        draw.rounded_rectangle(
            [bx, by, bx + bw, by + bh],
            radius=bh // 2,
            fill=palette.get('badge_bg', (0, 229, 255, 45)),
            outline=palette.get('badge_border', (0, 229, 255, 180)),
            width=1
        )

        # Glowing dot
        dot_r = 4
        dot_x = bx + pad_x + dot_r
        dot_y = by + (bh // 2)
        draw.ellipse(
            [dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r],
            fill=palette['accent']
        )

        # Badge text
        text_x = dot_x + dot_r + 8
        text_y = by + pad_y - 2
        draw.text((text_x, text_y), text, font=font, fill=palette.get('badge_text', (255, 255, 255)))

    @classmethod
    def _draw_title_and_artist_left(cls, draw, base_image, title, artist, max_width, x, bottom_y, palette):
        """Draws prominent left-aligned song title with artist name above it."""
        # 1. Artist font
        artist_font = cls._get_font('Arial', size=26, bold=True)
        artist_text = (artist or '').upper().strip()

        # 2. Dynamic font auto-scaling for title
        title_font, lines = cls._fit_text_multiline(title, max_width=max_width, max_lines=2, start_size=64, min_size=38)

        # Calculate heights
        line_height = int(title_font.size * 1.15) if hasattr(title_font, 'size') else 60
        total_title_h = len(lines) * line_height
        artist_h = 36 if artist_text else 0
        total_h = artist_h + total_title_h

        curr_y = bottom_y - total_h

        # Draw Artist Name
        if artist_text:
            # Shadow
            draw.text((x + 2, curr_y + 2), artist_text, font=artist_font, fill=(0, 0, 0, 220))
            # Text with accent tint
            draw.text((x, curr_y), artist_text, font=artist_font, fill=palette['accent'])
            curr_y += artist_h + 4

        # Draw Title Lines
        for line in lines:
            # Multi-layer drop shadow for maximum legibility against any background
            draw.text((x + 3, curr_y + 3), line, font=title_font, fill=(0, 0, 0, 250))
            draw.text((x + 1, curr_y + 1), line, font=title_font, fill=(0, 0, 0, 200))
            # Crisp white title
            draw.text((x, curr_y), line, font=title_font, fill=(255, 255, 255))
            curr_y += line_height

    @classmethod
    def _draw_title_and_artist_stacked(cls, draw, base_image, title, artist, x, y, max_width, max_height, palette):
        """Draws stacked typography on the right side of the album card."""
        artist_font = cls._get_font('Arial', size=24, bold=True)
        artist_text = (artist or '').upper().strip()

        title_font, lines = cls._fit_text_multiline(title, max_width=max_width, max_lines=3, start_size=52, min_size=32)
        line_height = int(title_font.size * 1.18) if hasattr(title_font, 'size') else 50

        curr_y = y

        if artist_text:
            draw.text((x + 2, curr_y + 2), artist_text, font=artist_font, fill=(0, 0, 0, 220))
            draw.text((x, curr_y), artist_text, font=artist_font, fill=palette['accent'])
            curr_y += 38

        for line in lines:
            draw.text((x + 3, curr_y + 3), line, font=title_font, fill=(0, 0, 0, 250))
            draw.text((x, curr_y), line, font=title_font, fill=(255, 255, 255))
            curr_y += line_height

    @classmethod
    def _draw_title_and_artist_centered(cls, draw, base_image, title, artist, max_width, center_y, palette):
        """Draws centered centerpiece title and artist."""
        artist_font = cls._get_font('Arial', size=28, bold=True)
        artist_text = (artist or '').upper().strip()

        title_font, lines = cls._fit_text_multiline(title, max_width=max_width, max_lines=2, start_size=72, min_size=42)
        line_height = int(title_font.size * 1.15) if hasattr(title_font, 'size') else 65

        total_title_h = len(lines) * line_height
        artist_h = 42 if artist_text else 0
        total_h = artist_h + total_title_h

        curr_y = center_y - (total_h // 2)

        if artist_text:
            bbox = draw.textbbox((0, 0), artist_text, font=artist_font)
            aw = bbox[2] - bbox[0]
            ax = (CANVAS_WIDTH - aw) // 2
            draw.text((ax + 2, curr_y + 2), artist_text, font=artist_font, fill=(0, 0, 0, 220))
            draw.text((ax, curr_y), artist_text, font=artist_font, fill=palette['accent'])
            curr_y += artist_h + 8

        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=title_font)
            lw = bbox[2] - bbox[0]
            lx = (CANVAS_WIDTH - lw) // 2
            draw.text((lx + 4, curr_y + 4), line, font=title_font, fill=(0, 0, 0, 250))
            draw.text((lx + 1, curr_y + 1), line, font=title_font, fill=(0, 0, 0, 200))
            draw.text((lx, curr_y), line, font=title_font, fill=(255, 255, 255))
            curr_y += line_height

    @classmethod
    def _fit_text_multiline(cls, text, max_width, max_lines=2, start_size=64, min_size=32):
        """Auto-scales font size and word-wraps to fit comfortably within bounds."""
        words = (text or 'Untitled').split()
        if not words:
            words = ['Untitled']

        # Dummy draw for measurement
        dummy_img = Image.new('RGB', (100, 100))
        d_draw = ImageDraw.Draw(dummy_img)

        curr_size = start_size
        while curr_size >= min_size:
            font = cls._get_font('Impact' if curr_size > 44 else 'Arial', size=curr_size, bold=True)
            lines = []
            cur_line = []

            for w in words:
                test_line = " ".join(cur_line + [w])
                bbox = d_draw.textbbox((0, 0), test_line, font=font)
                w_px = bbox[2] - bbox[0]
                if w_px <= max_width:
                    cur_line.append(w)
                else:
                    if cur_line:
                        lines.append(" ".join(cur_line))
                        cur_line = [w]
                    else:
                        lines.append(w)
                        cur_line = []

            if cur_line:
                lines.append(" ".join(cur_line))

            if len(lines) <= max_lines:
                # Check max line width
                all_fit = all((d_draw.textbbox((0, 0), l, font=font)[2] - d_draw.textbbox((0, 0), l, font=font)[0]) <= max_width for l in lines)
                if all_fit:
                    return font, lines

            curr_size -= 4

        # Fallback with min_size
        font = cls._get_font('Arial', size=min_size, bold=True)
        return font, lines[:max_lines]

    # -------------------------------------------------------------------------
    # Procedural Image / Background Helpers
    # -------------------------------------------------------------------------
    @classmethod
    def _load_and_cover_fill(cls, img_path, target_w, target_h):
        """Loads and crops/resizes image to fill 1280x720 (cover mode)."""
        src = Image.open(img_path).convert('RGB')
        src_w, src_h = src.size

        scale = max(target_w / src_w, target_h / src_h)
        new_w = int(src_w * scale)
        new_h = int(src_h * scale)

        resized = src.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        return resized.crop((left, top, left + target_w, top + target_h))

    @classmethod
    def _generate_mesh_gradient(cls, width, height, palette):
        """Generates rich radial / directional gradient with ambient glow."""
        img = Image.new('RGBA', (width, height), (0, 0, 0, 255))
        draw = ImageDraw.Draw(img)

        c1 = palette['bg_start']
        c2 = palette['bg_end']
        accent = palette['accent']

        # Base diagonal linear gradient
        for y in range(height):
            ratio = y / height
            r = int(c1[0] + (c2[0] - c1[0]) * ratio)
            g = int(c1[1] + (c2[1] - c1[1]) * ratio)
            b = int(c1[2] + (c2[2] - c1[2]) * ratio)
            draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

        # Ambient Top-Right Accent Light Glow
        glow = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        g_draw = ImageDraw.Draw(glow)
        center_x = int(width * 0.85)
        center_y = int(height * 0.25)
        max_rad = int(height * 0.75)

        for rad in range(max_rad, 0, -8):
            alpha = int((1.0 - (rad / max_rad)) ** 2 * 90)
            g_draw.ellipse(
                [center_x - rad, center_y - rad, center_x + rad, center_y + rad],
                fill=(accent[0], accent[1], accent[2], alpha)
            )

        glow = glow.filter(ImageFilter.GaussianBlur(radius=25))
        return Image.alpha_composite(img, glow)

    @classmethod
    def _draw_decorative_soundwave(cls, draw, width, height, palette):
        """Draws subtle modern soundwaves in the background."""
        accent = palette['accent']
        base_y = int(height * 0.78)
        bar_count = 32
        bar_width = 8
        spacing = 18
        total_w = bar_count * spacing
        start_x = (width - total_w) // 2

        for i in range(bar_count):
            norm = (i - (bar_count / 2)) / (bar_count / 2)
            h = int(math.sin(i * 0.4) * 22 + math.cos(i * 0.7) * 15 + 35) * (1.0 - abs(norm) * 0.5)
            x = start_x + i * spacing
            alpha = int(120 * (1.0 - abs(norm) * 0.6))
            draw.rounded_rectangle(
                [x, base_y - (h // 2), x + bar_width, base_y + (h // 2)],
                radius=4,
                fill=(accent[0], accent[1], accent[2], alpha)
            )

    @classmethod
    def _draw_procedural_vinyl_card(cls, base_img, x, y, size, palette):
        """Draws a stylized procedural vinyl disc / cover artwork card."""
        card = Image.new('RGBA', (size, size), (18, 20, 30, 255))
        draw = ImageDraw.Draw(card)

        # Concentric vinyl grooves
        center = size // 2
        for r in range(size // 2 - 20, 50, -12):
            draw.ellipse([center - r, center - r, center + r, center + r], outline=(255, 255, 255, 15), width=1)

        # Center label
        label_r = 70
        draw.ellipse([center - label_r, center - label_r, center + label_r, center + label_r], fill=palette['bg_end'], outline=palette['accent'], width=2)
        draw.ellipse([center - 12, center - 12, center + 12, center + 12], fill=(8, 10, 15, 255))

        base_img.paste(card, (x, y))
