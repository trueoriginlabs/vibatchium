"""Opt-in stealth layers on top of Patchright's baseline.

These integrations are gated behind separate installs because the underlying
libraries either aren't on PyPI (CDP-Patches, GPL-3.0) or have licensing
wrinkles. Vibatchium's base stealth (Patchright's Runtime.enable patch, real
Chrome channel, persistent context) is enough for most Cloudflare-class targets;
these add-ons target the harder defenders (Brotector, DataDome aggressive mode,
Akamai Bot Manager).

Install CDP-Patches separately (no pip extra; PyPI rejects git+https deps):
    pip install git+https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches.git@main

Usage from CLI:
    vb start --stealth-mouse   # humanize mouse trajectories

If CDP-Patches isn't installed, `vb start --stealth-mouse` emits a clear
error message with the install command rather than silently degrading.
"""
from .mouse import humanize_mouse_available, install_humanized_mouse

__all__ = ["humanize_mouse_available", "install_humanized_mouse"]
