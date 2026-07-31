"""Display the repository's original BART splash PNG on the TFT framebuffer.

The source PNG is loaded directly and is never rewritten. At display time it is
scaled proportionally to fit the 480x320 panel, centered on black, converted to
the framebuffer's RGB565 format, and written once to /dev/fb0.
"""

import argparse
import os
import sys
import time
from pathlib import Path


DISPLAY_WIDTH = 480
DISPLAY_HEIGHT = 320
FRAMEBUFFER_BYTES = DISPLAY_WIDTH * DISPLAY_HEIGHT * 2


def _scale_to_fit(
        source_width,
        source_height,
        target_width=DISPLAY_WIDTH,
        target_height=DISPLAY_HEIGHT):
    """Return the largest centered size that preserves the source aspect ratio."""
    dimensions = (
        source_width,
        source_height,
        target_width,
        target_height,
    )
    if any(value <= 0 for value in dimensions):
        raise ValueError('Image and display dimensions must be positive')

    if source_width * target_height >= source_height * target_width:
        width = target_width
        height = max(
            1,
            (source_height * target_width + source_width // 2)
            // source_width,
        )
    else:
        height = target_height
        width = max(
            1,
            (source_width * target_height + source_height // 2)
            // source_height,
        )
    return width, height


def _wait_for_framebuffer(path, timeout_seconds, poll_seconds=0.1):
    """Wait a bounded amount of time for the TFT framebuffer to appear."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        if path.exists():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_seconds, remaining))


def _render_rgb565(image_path):
    """Decode the exact PNG and return one 480x320 RGB565 framebuffer frame."""
    os.environ.setdefault('SDL_VIDEODRIVER', 'offscreen')
    os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')

    # Keep these imports local so deployment/unit tests can inspect this module
    # on hosts that do not have the Pi's pygame runtime installed.
    import numpy as np
    import pygame

    pygame.init()
    try:
        source = pygame.image.load(str(image_path))
        scaled_size = _scale_to_fit(
            source.get_width(),
            source.get_height(),
        )
        scaled = pygame.transform.smoothscale(source, scaled_size)

        canvas = pygame.Surface(
            (DISPLAY_WIDTH, DISPLAY_HEIGHT),
            depth=32,
        )
        canvas.fill((0, 0, 0))
        canvas.blit(
            scaled,
            (
                (DISPLAY_WIDTH - scaled_size[0]) // 2,
                (DISPLAY_HEIGHT - scaled_size[1]) // 2,
            ),
        )

        pixels = pygame.surfarray.array3d(canvas).transpose(1, 0, 2)
        red = pixels[:, :, 0].astype(np.uint16)
        green = pixels[:, :, 1].astype(np.uint16)
        blue = pixels[:, :, 2].astype(np.uint16)
        rgb565 = (
            ((red & 0xF8) << 8)
            | ((green & 0xFC) << 3)
            | (blue >> 3)
        )
        frame = rgb565.astype('<u2', copy=False).tobytes()
        if len(frame) != FRAMEBUFFER_BYTES:
            raise RuntimeError(
                f'Expected {FRAMEBUFFER_BYTES} framebuffer bytes, '
                f'got {len(frame)}'
            )
        return frame
    finally:
        pygame.quit()


def _write_all(path, data):
    """Write an entire rendered frame without invoking a shell."""
    descriptor = os.open(path, os.O_WRONLY)
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError('Framebuffer write made no progress')
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def show_splash(image_path, framebuffer_path, wait_seconds):
    """Render and display one splash frame, returning a process exit code."""
    if not image_path.is_file():
        print(
            f'[splash] image not found: {image_path}',
            file=sys.stderr,
        )
        return 1

    if not _wait_for_framebuffer(framebuffer_path, wait_seconds):
        print(
            f'[splash] framebuffer not found after {wait_seconds:g}s: '
            f'{framebuffer_path}',
            file=sys.stderr,
        )
        return 1

    try:
        frame = _render_rgb565(image_path)
        _write_all(framebuffer_path, frame)
    except Exception as error:
        print(f'[splash] could not display image: {error}', file=sys.stderr)
        return 1

    print(f'[splash] displayed original image from {image_path}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Show the original BART splash PNG on /dev/fb0.',
    )
    parser.add_argument(
        '--image',
        type=Path,
        required=True,
        help='Path to the unchanged source PNG.',
    )
    parser.add_argument(
        '--framebuffer',
        type=Path,
        default=Path('/dev/fb0'),
        help='Framebuffer device to write (default: /dev/fb0).',
    )
    parser.add_argument(
        '--wait-seconds',
        type=float,
        default=15.0,
        help='Maximum wait for the framebuffer device (default: 15).',
    )
    args = parser.parse_args(argv)
    return show_splash(
        args.image,
        args.framebuffer,
        args.wait_seconds,
    )


if __name__ == '__main__':
    raise SystemExit(main())
