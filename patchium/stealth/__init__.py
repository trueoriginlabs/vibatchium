"""Opt-in stealth layers on top of Patchright's baseline.

These integrations are gated behind extras (`pip install patchium[stealth-mouse]`,
etc.) because they pull in deps that aren't on PyPI (CDP-Patches) or have
licensing wrinkles. Patchium's base stealth (Patchright's Runtime.enable patch,
real Chrome channel, persistent context) is enough for most Cloudflare-class
targets; these add-ons target the harder defenders (Brotector, DataDome
aggressive mode, Akamai Bot Manager).

Usage from CLI:
    patchium start --stealth-mouse   # humanize mouse trajectories

If the corresponding extra isn't installed, start emits a clear error message
with the install command rather than silently degrading.
"""
from .mouse import humanize_mouse_available, install_humanized_mouse

__all__ = ["humanize_mouse_available", "install_humanized_mouse"]
