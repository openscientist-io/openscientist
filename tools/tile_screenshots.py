#!/usr/bin/env python3
"""
Screenshot Tiling Tool with Interaction Annotations

Creates a tiled image from multiple screenshots to visualize user flows.
Each screenshot is labeled with a step number and can include annotations
showing what was clicked, typed, or interacted with.
"""

import argparse
import json
import math
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont


def draw_click_indicator(
    draw: ImageDraw.Draw,
    x: int,
    y: int,
    radius: int = 20,
    color: tuple = (255, 87, 34),  # Orange-red
):
    """Draw a click indicator (concentric circles with a pointer)."""
    # Outer circle
    draw.ellipse(
        [x - radius, y - radius, x + radius, y + radius],
        outline=color,
        width=3,
    )
    # Inner circle
    inner_r = radius // 2
    draw.ellipse(
        [x - inner_r, y - inner_r, x + inner_r, y + inner_r],
        fill=color,
    )


def draw_cursor(
    draw: ImageDraw.Draw,
    x: int,
    y: int,
    size: int = 24,
    color: tuple = (0, 0, 0),
    outline: tuple = (255, 255, 255),
):
    """Draw a mouse cursor pointer."""
    # Cursor shape points (arrow)
    points = [
        (x, y),  # Tip
        (x, y + size),  # Bottom left
        (x + size * 0.35, y + size * 0.7),  # Inner
        (x + size * 0.5, y + size),  # Right bottom
        (x + size * 0.65, y + size * 0.85),  # Right middle
        (x + size * 0.4, y + size * 0.55),  # Inner right
        (x + size * 0.7, y + size * 0.4),  # Right point
    ]
    # Draw outline
    draw.polygon(points, fill=color, outline=outline, width=2)


def draw_type_indicator(
    draw: ImageDraw.Draw,
    x: int,
    y: int,
    width: int = 100,
    height: int = 30,
    color: tuple = (33, 150, 243),  # Blue
    font: Optional[ImageFont.FreeTypeFont] = None,
):
    """Draw a typing indicator (keyboard icon with text)."""
    # Draw a rounded rectangle
    draw.rounded_rectangle(
        [x, y, x + width, y + height],
        radius=5,
        fill=color,
    )
    # Draw keyboard icon (simplified)
    kb_x = x + 8
    kb_y = y + 8
    kb_w = 20
    kb_h = 14
    draw.rectangle([kb_x, kb_y, kb_x + kb_w, kb_y + kb_h], outline="white", width=1)
    # Keys
    for i in range(3):
        draw.rectangle(
            [kb_x + 2 + i * 7, kb_y + 2, kb_x + 6 + i * 7, kb_y + 5],
            fill="white",
        )
    draw.rectangle([kb_x + 4, kb_y + 8, kb_x + kb_w - 4, kb_y + 11], fill="white")

    # Text
    if font:
        draw.text((x + 35, y + 6), "TYPE", fill="white", font=font)


def draw_arrow(
    draw: ImageDraw.Draw,
    start: tuple,
    end: tuple,
    color: tuple = (255, 87, 34),
    width: int = 3,
    head_size: int = 12,
):
    """Draw an arrow from start to end."""
    import math as m

    # Draw line
    draw.line([start, end], fill=color, width=width)

    # Calculate arrow head
    angle = m.atan2(end[1] - start[1], end[0] - start[0])
    x, y = end

    # Arrow head points
    left_x = x - head_size * m.cos(angle - m.pi / 6)
    left_y = y - head_size * m.sin(angle - m.pi / 6)
    right_x = x - head_size * m.cos(angle + m.pi / 6)
    right_y = y - head_size * m.sin(angle + m.pi / 6)

    draw.polygon([(x, y), (left_x, left_y), (right_x, right_y)], fill=color)


def draw_annotation_badge(
    draw: ImageDraw.Draw,
    x: int,
    y: int,
    text: str,
    bg_color: tuple = (255, 87, 34),
    text_color: tuple = (255, 255, 255),
    font: Optional[ImageFont.FreeTypeFont] = None,
):
    """Draw a badge with text annotation."""
    # Calculate text size
    if font:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
    else:
        text_width = len(text) * 8
        text_height = 12

    padding = 8
    badge_width = text_width + padding * 2
    badge_height = text_height + padding

    # Draw badge background
    draw.rounded_rectangle(
        [x, y, x + badge_width, y + badge_height],
        radius=badge_height // 2,
        fill=bg_color,
    )

    # Draw text
    draw.text(
        (x + padding, y + padding // 2),
        text,
        fill=text_color,
        font=font,
    )

    return badge_width, badge_height


def annotate_image(
    img: Image.Image,
    annotations: list[dict],
    scale: float = 1.0,
) -> Image.Image:
    """
    Add annotations to an image.

    Annotation types:
    - click: {"type": "click", "x": 100, "y": 200, "label": "Click here"}
    - type: {"type": "type", "x": 100, "y": 200, "text": "user input"}
    - arrow: {"type": "arrow", "from": [x1, y1], "to": [x2, y2]}
    - badge: {"type": "badge", "x": 100, "y": 200, "text": "1"}
    """
    img = img.copy()
    draw = ImageDraw.Draw(img)

    # Load font
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except (OSError, IOError):
        font = ImageFont.load_default()
        small_font = font

    for ann in annotations:
        ann_type = ann.get("type", "click")
        x = int(ann.get("x", 0) * scale)
        y = int(ann.get("y", 0) * scale)

        if ann_type == "click":
            # Draw click indicator
            draw_click_indicator(draw, x, y)
            # Draw cursor
            draw_cursor(draw, x + 5, y + 5)
            # Draw label if provided
            if "label" in ann:
                draw_annotation_badge(
                    draw,
                    x + 30,
                    y - 10,
                    ann["label"],
                    bg_color=(255, 87, 34),
                    font=small_font,
                )

        elif ann_type == "type":
            # Draw typing indicator
            draw_type_indicator(draw, x, y, font=small_font)
            # Draw the typed text below
            if "text" in ann:
                text = ann["text"]
                if len(text) > 30:
                    text = text[:27] + "..."
                draw.rounded_rectangle(
                    [x, y + 35, x + 250, y + 55],
                    radius=3,
                    fill=(240, 240, 240),
                    outline=(200, 200, 200),
                )
                draw.text((x + 5, y + 38), f'"{text}"', fill=(50, 50, 50), font=small_font)

        elif ann_type == "arrow":
            start = (int(ann["from"][0] * scale), int(ann["from"][1] * scale))
            end = (int(ann["to"][0] * scale), int(ann["to"][1] * scale))
            draw_arrow(draw, start, end)

        elif ann_type == "badge":
            draw_annotation_badge(
                draw,
                x,
                y,
                ann.get("text", ""),
                bg_color=tuple(ann.get("color", [76, 175, 80])),
                font=font,
            )

        elif ann_type == "navigate":
            # Navigation indicator (arrow with URL)
            draw_annotation_badge(
                draw,
                x,
                y,
                f"-> {ann.get('to', '')}",
                bg_color=(103, 58, 183),  # Purple
                font=small_font,
            )

    return img


def add_rounded_corners(
    img: Image.Image,
    radius: int = 6,
    background_color: tuple = (245, 245, 245),
) -> Image.Image:
    """Add rounded corners to an image, returning RGBA for compositing."""
    # Create a mask with rounded corners
    mask = Image.new("L", img.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(
        [0, 0, img.width - 1, img.height - 1],
        radius=radius,
        fill=255,
    )

    # Convert to RGBA
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # Apply the mask as alpha channel
    output = Image.new("RGBA", img.size, (0, 0, 0, 0))
    output.paste(img, (0, 0))
    output.putalpha(mask)

    return output


def add_drop_shadow(
    img: Image.Image,
    offset: tuple = (4, 4),
    blur_radius: int = 8,
    shadow_color: tuple = (0, 0, 0, 60),
    background_color: tuple = (245, 245, 245),
) -> Image.Image:
    """Add a drop shadow to an RGBA image."""
    # Calculate new size to accommodate shadow
    shadow_offset_x, shadow_offset_y = offset
    new_width = img.width + abs(shadow_offset_x) + blur_radius * 2
    new_height = img.height + abs(shadow_offset_y) + blur_radius * 2

    # Create shadow layer
    shadow = Image.new("RGBA", (new_width, new_height), (0, 0, 0, 0))

    # Create shadow shape from image alpha
    if img.mode == "RGBA":
        shadow_shape = img.split()[3]  # Get alpha channel
    else:
        shadow_shape = Image.new("L", img.size, 255)

    # Position shadow
    shadow_x = blur_radius + max(0, shadow_offset_x)
    shadow_y = blur_radius + max(0, shadow_offset_y)

    # Create colored shadow
    shadow_colored = Image.new("RGBA", img.size, shadow_color)
    shadow_colored.putalpha(shadow_shape)

    shadow.paste(shadow_colored, (shadow_x, shadow_y))

    # Apply blur to shadow (simple box blur approximation)
    from PIL import ImageFilter

    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    # Create output with background
    output = Image.new("RGB", (new_width, new_height), background_color)

    # Composite shadow
    output.paste(shadow, (0, 0), shadow)

    # Paste original image on top
    img_x = blur_radius + max(0, -shadow_offset_x)
    img_y = blur_radius + max(0, -shadow_offset_y)
    output.paste(img, (img_x, img_y), img if img.mode == "RGBA" else None)

    return output


def draw_high_quality_circle(
    canvas: Image.Image,
    center_x: int,
    center_y: int,
    radius: int,
    fill_color: tuple,
    outline_color: tuple = (255, 255, 255),
    outline_width: int = 2,
) -> None:
    """Draw a high-quality anti-aliased circle by rendering at 4x and downscaling."""
    scale = 4
    size = (radius * 2 + outline_width * 2) * scale
    circle_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    circle_draw = ImageDraw.Draw(circle_img)

    # Draw at high resolution
    scaled_radius = radius * scale
    scaled_outline = outline_width * scale
    center = size // 2

    # Outline circle
    circle_draw.ellipse(
        [
            center - scaled_radius - scaled_outline // 2,
            center - scaled_radius - scaled_outline // 2,
            center + scaled_radius + scaled_outline // 2,
            center + scaled_radius + scaled_outline // 2,
        ],
        fill=outline_color,
    )
    # Fill circle
    circle_draw.ellipse(
        [
            center - scaled_radius + scaled_outline // 2,
            center - scaled_radius + scaled_outline // 2,
            center + scaled_radius - scaled_outline // 2,
            center + scaled_radius - scaled_outline // 2,
        ],
        fill=fill_color,
    )

    # Downscale with anti-aliasing
    final_size = size // scale
    circle_img = circle_img.resize((final_size, final_size), Image.Resampling.LANCZOS)

    # Paste onto canvas
    paste_x = center_x - final_size // 2
    paste_y = center_y - final_size // 2
    canvas.paste(circle_img, (paste_x, paste_y), circle_img)


def tile_screenshots(
    image_paths: list[Path],
    output_path: Path,
    annotations: Optional[list[list[dict]]] = None,
    descriptions: Optional[list[str]] = None,
    columns: int = 3,
    padding: int = 6,
    max_thumb_width: int = 400,
    max_thumb_height: int = 300,
    description_height: int = 35,
    footer_text: Optional[str] = None,
    title: Optional[str] = None,
    viewport: Optional[str] = None,
    browser: Optional[str] = None,
    test_status: Optional[str] = None,
    scale: float = 3.0,
) -> Path:
    """
    Create a tiled image from multiple screenshots with optional annotations.

    Args:
        image_paths: List of paths to screenshot images
        output_path: Path to save the tiled image
        annotations: List of annotation lists, one per image
        descriptions: List of description strings, one per image
        columns: Number of columns in the tile grid
        padding: Padding between images in pixels
        max_thumb_width: Maximum width of each thumbnail
        max_thumb_height: Maximum height of each thumbnail
        description_height: Height of description area below each image
        footer_text: Optional footer text (e.g., date, commit, branch)
        title: Optional title displayed at the top
        viewport: Optional viewport size (e.g., "375×812")
        browser: Optional browser name (e.g., "Chromium")
        test_status: Optional test status ("passed" or "failed")

    Returns:
        Path to the created tiled image
    """
    if not image_paths:
        raise ValueError("No images provided")

    # Load, annotate, and resize images
    thumbnails = []
    for idx, img_path in enumerate(image_paths):
        img = Image.open(img_path)

        # Apply annotations if provided
        if annotations and idx < len(annotations) and annotations[idx]:
            img = annotate_image(img, annotations[idx], scale=1.0)

        # Resize to exact size (crop/pad if needed to maintain consistency)
        # First resize maintaining aspect ratio
        ratio = min(max_thumb_width / img.width, max_thumb_height / img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        thumb = img.resize(new_size, Image.Resampling.LANCZOS)

        # Draw step number badge inside the thumbnail (top-left)
        step_num = str(idx + 1)
        badge_size = 24
        badge_margin = 6
        badge_x = badge_margin
        badge_y = badge_margin

        thumb_draw = ImageDraw.Draw(thumb)
        try:
            badge_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13
            )
        except (OSError, IOError):
            badge_font = ImageFont.load_default()

        # Draw badge circle - white fill with black border
        thumb_draw.ellipse(
            [badge_x, badge_y, badge_x + badge_size, badge_y + badge_size],
            fill=(255, 255, 255),
            outline=(0, 0, 0),
            width=1,
        )

        # Draw step number text
        bbox = thumb_draw.textbbox((0, 0), step_num, font=badge_font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        thumb_draw.text(
            (
                badge_x + (badge_size - text_w) // 2,
                badge_y + (badge_size - text_h) // 2 - 1,
            ),
            step_num,
            fill=(0, 0, 0),
            font=badge_font,
        )

        # Apply rounded corners (returns RGBA)
        thumb_rounded = add_rounded_corners(thumb, radius=8)

        # Create canvas - minimal margin
        thumb_canvas_w = thumb_rounded.width + 2
        thumb_canvas_h = thumb_rounded.height + 2
        fixed_thumb = Image.new("RGB", (thumb_canvas_w, thumb_canvas_h), (245, 245, 245))
        paste_x = 1
        paste_y = 1

        # Paste the rounded thumbnail
        fixed_thumb.paste(thumb_rounded, (paste_x, paste_y), thumb_rounded)

        # Draw 1px border with proper rounded corners
        border_draw = ImageDraw.Draw(fixed_thumb)
        border_draw.rounded_rectangle(
            [
                paste_x,
                paste_y,
                paste_x + thumb_rounded.width - 1,
                paste_y + thumb_rounded.height - 1,
            ],
            radius=8,
            outline=(200, 200, 200),
            width=1,
        )
        thumbnails.append((fixed_thumb, thumb_canvas_w, thumb_canvas_h))

    # Calculate grid dimensions
    rows = math.ceil(len(thumbnails) / columns)

    # Get actual thumbnail dimensions (including shadow margin)
    thumb_w = thumbnails[0][1] if thumbnails else max_thumb_width
    thumb_h = thumbnails[0][2] if thumbnails else max_thumb_height

    # Calculate header height (title + metadata)
    header_height = 0
    if title:
        header_height += 28
    if viewport or browser or test_status:
        header_height += 18

    # Calculate canvas size (minimal padding)
    cell_height = thumb_h + description_height
    footer_height = 20 if footer_text else 0
    half_padding = padding // 2  # Reduced padding between screenshots
    canvas_width = columns * thumb_w + (columns + 1) * half_padding
    canvas_height = (
        header_height
        + 2  # Minimal top padding
        + rows * cell_height
        + (rows - 1) * half_padding  # Reduced padding between rows
        + 2  # Minimal bottom margin before footer
        + footer_height
    )

    # Create canvas
    canvas = Image.new("RGB", (canvas_width, canvas_height), color=(245, 245, 245))
    draw = ImageDraw.Draw(canvas)

    # Load fonts
    try:
        num_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        desc_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 11)
    except (OSError, IOError):
        num_font = ImageFont.load_default()
        desc_font = num_font

    # Load additional fonts for header
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        meta_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    except (OSError, IOError):
        title_font = num_font
        meta_font = desc_font

    # Draw header (title and metadata)
    header_y = padding // 2
    if title:
        draw.text((padding, header_y), title, fill=(50, 50, 50), font=title_font)
        header_y += 22

    if viewport or browser or test_status:
        meta_parts = []
        if viewport:
            meta_parts.append(f"Viewport: {viewport}")
        if browser:
            meta_parts.append(f"Browser: {browser}")
        if test_status:
            status_text = test_status.upper()
            meta_parts.append(f"E2E Test: {status_text}")

        meta_text = "  •  ".join(meta_parts)

        # Draw metadata, with test status colored
        if test_status:
            # Draw parts before status
            pre_status = "  •  ".join(meta_parts[:-1])
            if pre_status:
                pre_status += "  •  "
            draw.text((padding, header_y), pre_status, fill=(100, 100, 100), font=meta_font)

            # Calculate position for status
            pre_bbox = draw.textbbox((0, 0), pre_status, font=meta_font)
            status_x = padding + pre_bbox[2] - pre_bbox[0]

            # Draw "E2E Test: " label
            label = "E2E Test: "
            draw.text((status_x, header_y), label, fill=(100, 100, 100), font=meta_font)
            label_bbox = draw.textbbox((0, 0), label, font=meta_font)
            status_x += label_bbox[2] - label_bbox[0]

            # Draw status with color
            status_color = (34, 139, 34) if test_status.lower() == "passed" else (220, 20, 60)
            draw.text((status_x, header_y), status_text, fill=status_color, font=meta_font)
        else:
            draw.text((padding, header_y), meta_text, fill=(100, 100, 100), font=meta_font)

    # Place images on canvas
    for idx, (thumb, tw, th) in enumerate(thumbnails):
        row = idx // columns
        col = idx % columns

        # Calculate position (account for header, reduced padding)
        cell_x = half_padding + col * (thumb_w + half_padding)
        cell_y = header_height + 2 + row * (cell_height + half_padding)

        # Paste thumbnail (step numbers already drawn inside)
        canvas.paste(thumb, (cell_x, cell_y))

        # Draw description below image (reduced spacing)
        if descriptions and idx < len(descriptions) and descriptions[idx]:
            desc_y = cell_y + thumb_h + 1  # Minimal gap
            desc_text = descriptions[idx]

            # Word wrap the description
            words = desc_text.split()
            lines = []
            current_line = ""
            for word in words:
                test_line = f"{current_line} {word}".strip()
                bbox = draw.textbbox((0, 0), test_line, font=desc_font)
                if bbox[2] - bbox[0] <= thumb_w - 10:
                    current_line = test_line
                else:
                    if current_line:
                        lines.append(current_line)
                    current_line = word
            if current_line:
                lines.append(current_line)

            # Draw lines
            for i, line in enumerate(lines[:3]):  # Max 3 lines
                draw.text(
                    (cell_x + 5, desc_y + i * 14),
                    line,
                    fill=(80, 80, 80),
                    font=desc_font,
                )

    # Draw footer if provided
    if footer_text:
        try:
            footer_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
        except (OSError, IOError):
            footer_font = ImageFont.load_default()

        footer_y = canvas_height - footer_height + 5
        draw.text(
            (padding, footer_y),
            footer_text,
            fill=(120, 120, 120),
            font=footer_font,
        )

    # Apply scale factor for higher DPI
    if scale != 1.0:
        new_width = int(canvas.width * scale)
        new_height = int(canvas.height * scale)
        canvas = canvas.resize((new_width, new_height), Image.Resampling.LANCZOS)

    # Save result
    canvas.save(output_path, quality=95)
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Create a tiled image from multiple screenshots with annotations"
    )
    parser.add_argument(
        "images",
        nargs="+",
        type=Path,
        help="Paths to screenshot images (in order)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("tiled_screenshots.png"),
        help="Output path for tiled image (default: tiled_screenshots.png)",
    )
    parser.add_argument(
        "-c",
        "--columns",
        type=int,
        default=3,
        help="Number of columns in grid (default: 3)",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=400,
        help="Maximum thumbnail width (default: 400)",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=300,
        help="Maximum thumbnail height (default: 300)",
    )
    parser.add_argument(
        "-a",
        "--annotations",
        type=Path,
        help="JSON file with annotations for each image",
    )
    parser.add_argument(
        "-f",
        "--footer",
        type=str,
        help="Footer text to display at bottom of image",
    )
    parser.add_argument(
        "--git-footer",
        action="store_true",
        help="Auto-generate footer with date, git branch, and commit",
    )
    parser.add_argument(
        "-p",
        "--padding",
        type=int,
        default=10,
        help="Padding between images in pixels (default: 10)",
    )
    parser.add_argument(
        "-t",
        "--title",
        type=str,
        help="Title displayed at the top of the image",
    )
    parser.add_argument(
        "--viewport",
        type=str,
        help="Viewport size (e.g., '375×812' or '1280×720')",
    )
    parser.add_argument(
        "--browser",
        type=str,
        default="Chromium",
        help="Browser name (default: Chromium)",
    )
    parser.add_argument(
        "--test-status",
        type=str,
        choices=["passed", "failed"],
        help="E2E test status (passed or failed)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=3.0,
        help="Scale factor for higher DPI output (default: 3.0)",
    )

    args = parser.parse_args()

    # Validate input files
    for img_path in args.images:
        if not img_path.exists():
            parser.error(f"Image not found: {img_path}")

    # Load annotations if provided
    annotations = None
    descriptions = None
    if args.annotations and args.annotations.exists():
        with open(args.annotations) as f:
            data = json.load(f)
            # Support both old format (list of annotation lists) and new format (dict with annotations and descriptions)
            if isinstance(data, dict):
                annotations = data.get("annotations")
                descriptions = data.get("descriptions")
            else:
                annotations = data

    # Generate footer text
    footer_text = args.footer
    if args.git_footer:
        import subprocess
        from datetime import datetime

        try:
            commit = (
                subprocess.check_output(
                    ["git", "rev-parse", "--short", "HEAD"],
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
            branch = (
                subprocess.check_output(
                    ["git", "branch", "--show-current"],
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
            date = datetime.now().strftime("%Y-%m-%d %H:%M")
            footer_text = f"Created: {date} | Branch: {branch} | Commit: {commit}"
        except (subprocess.CalledProcessError, FileNotFoundError):
            # Git not available, skip footer
            pass

    result = tile_screenshots(
        image_paths=args.images,
        output_path=args.output,
        annotations=annotations,
        descriptions=descriptions,
        columns=args.columns,
        padding=args.padding,
        max_thumb_width=args.max_width,
        max_thumb_height=args.max_height,
        footer_text=footer_text,
        title=args.title,
        viewport=args.viewport,
        browser=args.browser,
        test_status=args.test_status,
        scale=args.scale,
    )
    print(f"Created tiled image: {result}")


if __name__ == "__main__":
    main()
