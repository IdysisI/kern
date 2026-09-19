"""Clipboard image capture — cross-platform, stdlib only.

Returns the same media dict shape the read tool produces:
    {"type": "image", "mime": "image/png", "data": "<base64>"}

Source priority (first hit wins):
  - Linux Wayland : wl-paste (--type image/* negotiation, then png fallback)
  - Linux X11     : xclip -selection clipboard -t image/png -o
  - macOS         : osascript (clipboard as «class PNGf»)
  - Windows       : powershell + System.Windows.Forms.Clipboard

No PIL/pyperclip dependency (neither is installed in the kern env).
Errors return (None, reason) — callers show the reason, never a traceback.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 8 MB decoded cap. Anthropic's wire limit is 5 MB per image; OpenAI's is
# ~20 MB but tile-heavy. 8 MB decoded keeps the base64 payload (~10.7 MB)
# under the 32 MB websocket frame used by daemon/TUI with room for text.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_TIMEOUT = 8  # seconds — clipboard daemons are local, this is generous


def _finish(raw: bytes, mime: str = "image/png") -> tuple[dict | None, str]:
    if not raw:
        return None, "clipboard has no image data"
    if len(raw) > MAX_IMAGE_BYTES:
        return None, (f"image too large ({len(raw) / 1e6:.1f} MB > "
                      f"{MAX_IMAGE_BYTES / 1e6:.0f} MB cap) — shrink it or save "
                      f"to a file and reference the path")
    return {"type": "image", "mime": mime, "data": base64.b64encode(raw).decode()}, ""


def _run(cmd: list[str], **kw) -> tuple[int, bytes]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT, **kw)
        return r.returncode, r.stdout or b""
    except FileNotFoundError:
        return 127, b""
    except subprocess.TimeoutExpired:
        return 124, b""


def _wl_paste() -> tuple[dict | None, str]:
    """Wayland: query offered MIME types, prefer png, accept any image/*."""
    rc, types = _run(["wl-paste", "--list-types"])
    if rc == 127:
        return None, "wl-paste not found (install wl-clipboard)"
    offered = [t.strip() for t in types.decode(errors="replace").splitlines() if t.strip()]
    if "image/png" in offered:
        pick = "image/png"
    else:
        pick = next((t for t in offered if t.startswith("image/")), None)
        if pick is None:
            return None, "clipboard holds no image (offered: " + ", ".join(offered[:6]) + ")"
    rc, raw = _run(["wl-paste", "--type", pick, "--no-newline"])
    if rc != 0:
        return None, f"wl-paste failed (exit {rc})"
    return _finish(raw, pick)


def _xclip() -> tuple[dict | None, str]:
    for tool in ("xclip",):
        if not shutil.which(tool):
            return None, "xclip not found"
        rc, raw = _run([tool, "-selection", "clipboard", "-t", "image/png", "-o"])
        if rc == 0:
            return _finish(raw, "image/png")
    return None, "no image/png on X11 clipboard"


def _osascript() -> tuple[dict | None, str]:
    script = ('set f to (open for access (POSIX file "%s") with write permission)\n'
              'set img to the clipboard as «class PNGf»\n'
              'write img to f\nclose access f\n')
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "clip.png"
        script = script % p
        rc, _ = _run(["osascript", "-e", script])
        if rc != 0 or not p.exists():
            return None, "no image on macOS clipboard"
        return _finish(p.read_bytes(), "image/png")


def _powershell() -> tuple[dict | None, str]:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$i=[System.Windows.Forms.Clipboard]::GetImage();"
        "if($i -eq $null){exit 1};"
        f"$i.Save('{tempfile.gettempdir()}\\kern_clip.png',"
        "[System.Drawing.Imaging.ImageFormat]::Png)"
    )
    out = Path(tempfile.gettempdir()) / "kern_clip.png"
    if out.exists():
        out.unlink()
    rc, _ = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    if rc != 0 or not out.exists():
        return None, "no image on Windows clipboard"
    try:
        return _finish(out.read_bytes(), "image/png")
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def has_image() -> bool:
    """Cheap probe: does the clipboard hold an image? Used by key handlers
    to decide between image-attach and plain-text paste WITHOUT grabbing
    (and base64-encoding) the payload. Linux negotiates via offered MIME
    types only; macOS/Windows fall back to a real grab (still local/fast)."""
    if sys.platform == "darwin" or sys.platform == "win32":
        img, _ = grab_image()
        return img is not None
    if shutil.which("wl-paste"):
        rc, types = _run(["wl-paste", "--list-types"])
        if rc != 0:
            return False
        return any(t.strip().startswith("image/")
                   for t in types.decode(errors="replace").splitlines())
    if shutil.which("xclip"):
        rc, targets = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
        if rc != 0:
            return False
        return b"image/" in targets
    return False


def grab_image() -> tuple[dict | None, str]:
    """Grab an image from the system clipboard.

    Returns (media_dict, "") on success, or (None, human_readable_reason).
    """
    if sys.platform == "darwin":
        return _osascript()
    if sys.platform == "win32":
        return _powershell()
    # Linux/BSD: Wayland first, then X11.
    if shutil.which("wl-paste"):
        return _wl_paste()
    if shutil.which("xclip"):
        return _xclip()
    return None, "no clipboard tool found (install wl-clipboard or xclip)"


async def has_image_async() -> bool:
    """Non-blocking has_image(): the probe spawns subprocesses (clipboard
    daemons can stall for seconds); running it on the TUI event loop froze
    the whole UI (audit r3 F1)."""
    import asyncio
    return await asyncio.to_thread(has_image)


async def grab_image_async() -> tuple[dict | None, str]:
    """Non-blocking grab_image() for the same reason."""
    import asyncio
    return await asyncio.to_thread(grab_image)
