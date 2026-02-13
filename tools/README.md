# Development Tools

This directory contains helper tools for development, testing, and documentation.

## tile_screenshots.py

Creates tiled images from multiple screenshots to visualize user flows. Useful for documentation, bug reports, and demonstrating UI interactions.

### Features

- Tiles multiple screenshots into a grid layout
- Adds numbered step labels in descriptions (e.g., "Step 1: ...")
- Supports interaction annotations (clicks, typing, badges, arrows)
- Adds descriptions below each screenshot
- Configurable thumbnail sizes and grid columns

### Usage

```bash
# Basic usage - tile screenshots into a grid
uv run python tools/tile_screenshots.py \
  screenshot1.png screenshot2.png screenshot3.png \
  -o output_tiled.png \
  -a annotations.json

# With custom layout
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
| `-a, --annotations` | JSON file with annotations (required) | None |
| `--git-footer` | Auto-generate footer with date, branch, commit | False |

### Annotations Format (Required)

**Annotations are required.** Create a JSON file with metadata, descriptions, and interaction annotations:

```json
{
  "metadata": {
    "viewport": "1280x800",
    "browser": "Chromium"
  },
  "descriptions": [
    "Jobs dashboard - view existing jobs and create new ones",
    "New job form - enter research question and configure settings",
    "Form filled with research question, max iterations set to 2"
  ],
  "annotations": [
    [{"type": "click", "x": 936, "y": 36, "label": "New Job"}],
    [{"type": "click", "x": 640, "y": 285, "label": "Enter question"}],
    [
      {"type": "type", "x": 400, "y": 236, "text": "Research question..."},
      {"type": "click", "x": 640, "y": 695, "label": "Start Discovery"}
    ]
  ]
}
```

**Note:** Step numbers are automatically added by the script. Just provide the description text without "Step N:" prefix.

**Required fields:**
- `descriptions`: List of description strings, one per image (required, non-empty)
- `annotations`: List of annotation lists for click/type indicators on images

**Optional fields:**
- `metadata`: Object with viewport, browser (displayed in header)

### Annotation Types

| Type | Required Fields | Optional Fields | Description |
|------|----------------|-----------------|-------------|
| `click` | `x`, `y` | `label` | Shows click indicator with cursor |
| `type` | `x`, `y` | `text` | Shows typing indicator with keyboard icon |
| `badge` | `x`, `y`, `text` | `color` (RGB array) | Shows colored badge with text |
| `arrow` | `from` [x,y], `to` [x,y] | | Draws arrow between two points |

### Capturing Screenshots with Playwright

**Efficient multi-viewport workflow:** For each step in the flow, capture screenshots at all viewport sizes before proceeding to the next step. This avoids running the entire flow multiple times.

1. **At each step:**
   - Take screenshot at desktop viewport (e.g., 1280x800)
   - Resize viewport to mobile (e.g., 390x844)
   - Take screenshot at mobile viewport
   - Resize back to desktop
   - Proceed to next step in the flow

2. **Record element positions** using `browser_evaluate` before clicking:
   ```javascript
   // Get element position for annotation
   () => {
     const btn = document.querySelector('button');
     const rect = btn.getBoundingClientRect();
     return {
       x: Math.round(rect.left + rect.width / 2),
       y: Math.round(rect.top + rect.height / 2)
     };
   }
   ```

3. **Save positions** to the annotations JSON file with the appropriate annotation type (`click`, `type`, etc.)

4. **Generate tiled images** for each viewport size:
   ```bash
   # Desktop flow
   uv run python tools/tile_screenshots.py \
     flow_screenshots/01_*.png \
     -o flow_screenshots/desktop_flow.png \
     -a flow_screenshots/annotations.json \
     --git-footer

   # Mobile flow
   uv run python tools/tile_screenshots.py \
     flow_screenshots/mobile_01_*.png \
     -o flow_screenshots/mobile_flow.png \
     -a flow_screenshots/mobile_annotations.json \
     --git-footer
   ```

### Output Examples

The tool generates images like:
- `flow_screenshots/job_creation_flow.png` - Desktop flow (1280x800 captures)
- `flow_screenshots/mobile_job_creation_flow.png` - Mobile flow (390x844 captures)
