"""BART Platform Display — entry point.

Wires together config, display, the background departures fetch, touch input, and
the swipe-down settings panel. On the Pi, touch comes from the XPT2046 evdev
device; with BART_DEV=1 the mouse stands in so the UI can be built on a desktop.
"""

import pygame

from bartdisplay import board, config, departures, display, touch
from bartdisplay.touch import RawEvent, GestureRecognizer, TouchReader, TOP_EDGE
from bartdisplay.ui.settings import SettingsPanel

FPS_IDLE = 10   # board-only: spare the Pi Zero W's CPU
FPS_ACTIVE = 30  # while the panel is open/animating: responsive touch


def _dev_mouse_events():
    """Translate pygame mouse events into raw touch events for dev mode."""
    raws = []
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            raws.append('quit')
        elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            raws.append('quit')
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            raws.append(RawEvent('down', *event.pos))
        elif event.type == pygame.MOUSEMOTION and event.buttons[0]:
            raws.append(RawEvent('move', *event.pos))
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            raws.append(RawEvent('up', *event.pos))
    return raws


def main():
    config.load()
    display.init()
    departures.start()

    gestures = GestureRecognizer()
    reader = None
    if not display.DEV:
        reader = TouchReader().start()

    panel = SettingsPanel()
    clock = pygame.time.Clock()
    blink = True
    blink_ms = 0
    running = True

    while running:
        fps = FPS_ACTIVE if panel.is_active() else FPS_IDLE
        dt = clock.tick(fps)

        blink_ms += dt
        if blink_ms >= 650:
            blink = not blink
            blink_ms = 0

        # Gather raw pointer events from touch (Pi) or mouse (dev).
        if display.DEV:
            raws = _dev_mouse_events()
        else:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
            raws = reader.poll() if reader else []

        for raw in raws:
            if raw == 'quit':
                running = False
                continue
            for sem in gestures.feed(raw):
                if panel.is_active():
                    panel.handle(sem)
                elif sem.get('kind') == 'release' and sem.get('swipe') == 'down' \
                        and sem.get('start_y', 999) < TOP_EDGE:
                    panel.open()

        panel.update(dt)
        if panel.request_exit:
            running = False  # systemd Restart=always relaunches with the new key
            continue

        rows, loading = departures.snapshot()
        board.render(rows, loading, blink)
        panel.render()
        display.present()

    pygame.quit()


if __name__ == '__main__':
    main()
