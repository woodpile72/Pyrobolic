# SPDX-License-Identifier: GPL-3.0-only
# Copyright 2026 Erick Woods
"""Best-effort X11 placement across tray hides; Wayland owns placement itself.

Capture/restore run on the window's action executor, never on the GTK thread.
Coordinates remain in X server pixels, independent of GTK's scale factor.
"""


def surface_id(window):
    import gi
    try:
        gi.require_version("GdkX11", "4.0")
        from gi.repository import GdkX11
        surface = window.get_surface()
        if isinstance(surface, GdkX11.X11Surface):
            return surface.get_xid()
    except (ImportError, ValueError):
        pass
    return None


def capture(identifier):
    if identifier is None:
        return None
    connection = None
    try:
        from Xlib.display import Display
        connection = Display()
        window = connection.create_resource_object("window", identifier)
        root = connection.screen().root
        coordinates = root.translate_coords(window, 0, 0)
        return coordinates.x, coordinates.y
    except Exception:
        # A disappearing surface/display must not prevent close or safe shutdown.
        return None
    finally:
        if connection:
            connection.close()


def restore(identifier, position):
    if identifier is None or position is None:
        return
    connection = None
    try:
        from Xlib import X, protocol
        from Xlib.display import Display
        connection = Display()
        root = connection.screen().root
        x, y = position
        # StaticGravity uses the saved client origin without guessing decoration sizes.
        flags = X.StaticGravity | (1 << 8) | (1 << 9) | (1 << 12)
        event = protocol.event.ClientMessage(window=identifier,
                                             client_type=connection.intern_atom("_NET_MOVERESIZE_WINDOW"),
                                             data=(32, [flags, x & 0xffffffff, y & 0xffffffff, 0, 0]))
        root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        connection.flush()
    except Exception:
        pass
    finally:
        if connection:
            connection.close()
