"""Deployment branding stays outside the public source tree."""
from html import escape
import json
import os
from pathlib import Path
import re

from fastapi.responses import HTMLResponse


def logo_path():
    return Path(os.getenv("BR_BRANDING_DIR", "config/branding")) / "logo.png"


def branch_names():
    path = logo_path().parent / "sites.json"
    try:
        if path.stat().st_size > 32768:
            return []
        names = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(names, list) or len(names) > 100:
        return []
    if any(not isinstance(name, str) or not 2 <= len(name.strip()) <= 80 for name in names):
        return []
    unique = {}
    for name in names:
        unique.setdefault(name.strip().casefold(), name.strip())
    return list(unique.values())


def branch_directory(names):
    if not names:
        return ""
    items = "".join(f'<li><span class="branch-number">{number:02}</span><span>{escape(name)}</span></li>'
                    for number, name in enumerate(names, 1))
    return ('<section class="branch-directory" aria-labelledby="branch-title">'
            '<div class="branch-heading"><div><div class="branch-eyebrow">PRESENCIA Y COBERTURA</div>'
            '<h2 id="branch-title">Nuestras sucursales</h2></div>'
            f'<span class="branch-total">{len(names)} sucursales</span></div>'
            f'<ul class="branch-list">{items}</ul>'
            '<p class="branch-caption">Directorio de ubicaciones. Los equipos y su estado se muestran en el inventario.</p></section>')


def page(path):
    organization = escape(os.getenv("BR_ORGANIZATION", "BloodRaven"), quote=True)
    region = escape(os.getenv("BR_REGION", "INFRAESTRUCTURA"), quote=True)
    logo = (f'<img src="/static/brand-logo.png" alt="{organization}">'
            if logo_path().is_file() else '<div class="brand-wordmark">BLOODRAVEN<span>MONITOR</span></div>')
    names = branch_names()
    private_page = path.name in {"index.html", "manage.html"}
    values = {"BR_ORGANIZATION": organization, "BR_REGION": region, "BR_LOGO": logo,
              "BR_BRANCH_DIRECTORY": branch_directory(names) if private_page else "",
              "BR_SITE_OPTIONS": "".join(f'<option value="{escape(name, quote=True)}"></option>' for name in names) if private_page else ""}
    # One substitution pass: configured text cannot become a template instruction.
    content = re.sub(r"\{\{(" + "|".join(values) + r")\}\}",
                     lambda match: values[match[1]], path.read_text(encoding="utf-8"))
    return HTMLResponse(content)
