# Development Tools

This directory contains helper tools for development, testing, and documentation.

## tile_screenshots.py

Creates tiled images from multiple screenshots to visualize user flows. Useful for documentation, bug reports, and demonstrating UI interactions.

### Features

- Tiles multiple screenshots into a grid layout
- Adds numbered step indicators (high-quality anti-aliased circles)
- Supports interaction annotations (clicks, typing, badges, arrows)
- Adds descriptions below each screenshot
- Configurable thumbnail sizes and grid columns

### Usage

```bash
# Basic usage - tile screenshots into a grid
uv run python tools/tile_screenshots.py \
  screenshot1.png screenshot2.png screenshot3.png \
  -o output_tiled.png

# With annotations and custom layout
uv run python tools/tile_screenshots.py \
  flow_screenshots/*.png \
  -o flow_screenshots/tiled_output.png \
  -a flow_screenshots/annotations.json \
  -c 2 \
  --max-width 600 \
  --max-height 400
```

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `-o, --output` | Output file path | `tiled_screenshots.png` |
| `-c, --columns` | Number of columns in grid | 3 |
| `--max-width` | Maximum thumbnail width (px) | 400 |
| `--max-height` | Maximum thumbnail height (px) | 300 |
| `-a, --annotations` | JSON file with annotations | None |

### Annotations Format

Create a JSON file with annotations and descriptions:

```json
{
  "annotations": [
    [
      {"type": "click", "x": 640, "y": 475, "label": "Click"}
    ],
    [
      {"type": "badge", "x": 180, "y": 130, "text": "Error visible!", "color": [244, 67, 54]}
    ],
    [
      {"type": "type", "x": 100, "y": 200, "text": "User input here"}
    ]
  ],
  "descriptions": [
    "Step 1: Click the login button",
    "Step 2: Error message appears",
    "Step 3: Enter text in the field"
  ]
}
```

### Annotation Types

| Type | Required Fields | Optional Fields | Description |
|------|----------------|-----------------|-------------|
| `click` | `x`, `y` | `label` | Shows click indicator with cursor |
| `type` | `x`, `y` | `text` | Shows typing indicator with keyboard icon |
| `badge` | `x`, `y`, `text` | `color` (RGB array) | Shows colored badge with text |
| `arrow` | `from` [x,y], `to` [x,y] | | Draws arrow between two points |

### Optimal Usage

1. **Capture screenshots at consistent viewport sizes** (e.g., 1280x720 for desktop, 375x812 for mobile)
2. **Use Playwright MCP** to get accurate element positions via `browser_evaluate`
3. **Record positions** when clicking elements for accurate annotations
4. **Keep descriptions concise** - they wrap to fit thumbnail width

### Example Workflow

```bash
# 1. Capture screenshots with Playwright at desktop size
# 2. Record click positions using browser_evaluate
# 3. Create annotations.json with positions and descriptions
# 4. Generate tiled image

uv run python tools/tile_screenshots.py \
  flow_screenshots/01_login.png \
  flow_screenshots/02_jobs_with_error.png \
  -o flow_screenshots/config_error_flow.png \
  -a flow_screenshots/annotations.json \
  -c 2 \
  --max-width 600 \
  --max-height 340
```

### Output Examples

The tool generates images like:
- `flow_screenshots/config_error_flow.png` - Desktop flow (1280x720 captures)
- `flow_screenshots/mobile_config_error_flow.png` - Mobile flow (375x812 captures)
