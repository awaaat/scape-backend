from django.core.checks import Warning, register

from .app_registry import base_url_for, valid_sources


@register()
def scape_apps_check(app_configs, **kwargs):
    issues = []
    seen = {}
    for source in valid_sources():
        base = base_url_for(source)
        if not base:
            issues.append(Warning(
                f"Scape app '{source}' has no base_url, so its emails will have broken links.",
                hint="Set it in settings.SCAPE_APPS (env var in the settings block).",
                id="users.W001",
            ))
            continue
        if base in seen:
            issues.append(Warning(
                f"Scape apps '{seen[base]}' and '{source}' share the same base_url ({base}); "
                f"emails for '{source}' will link to '{seen[base]}'.",
                hint="Set a distinct base URL for each app (e.g. PORTAL_BASE_URL, THESIS_BASE_URL in .env).",
                id="users.W002",
            ))
        seen.setdefault(base, source)
    return issues
