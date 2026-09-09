"""Deployment branding stays outside the public source tree."""
from html import escape
import os
from pathlib import Path
import re

from fastapi.responses import HTMLResponse


def logo_path():
    return Path(os.getenv("BR_BRANDING_DIR", "config/branding")) / "logo.png"


def page(path):
    organization = escape(os.getenv("BR_ORGANIZATION", "BloodRaven"), quote=True)
    region = escape(os.getenv("BR_REGION", "INFRAESTRUCTURA"), quote=True)
    logo = (f'<img src="/static/brand-logo.png" alt="{organization}">'
            if logo_path().is_file() else '<div class="brand-wordmark">BLOODRAVEN<span>MONITOR</span></div>')
    values = {"BR_ORGANIZATION": organization, "BR_REGION": region, "BR_LOGO": logo}
    # One substitution pass: configured text cannot become a template instruction.
    content = re.sub(r"\{\{(BR_ORGANIZATION|BR_REGION|BR_LOGO)\}\}",
                     lambda match: values[match[1]], path.read_text(encoding="utf-8"))
    return HTMLResponse(content)
