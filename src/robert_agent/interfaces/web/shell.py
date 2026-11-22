"""Shared Workbench chrome for the HTML views."""

_HEAD = """<link rel="icon" type="image/svg+xml" href="/assets/robert-favicon.svg">
  <script src="/assets/shell-preferences.js"></script>"""

_BRAND = """<a class="brand" href="/" aria-label="Robert Workbench">
      <span class="brand-mark" aria-hidden="true"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="6" cy="5" r="2.5"/><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M6 7.5v9M18 8.5v2a6 6 0 0 1-6 6H6"/></svg></span>
      <span>Robert</span>
    </a>"""

_PREFERENCES = """<label class="theme-control"><span class="sr-only" data-i18n="theme">Theme</span>
        <select id="theme-select" aria-label="Theme" data-i18n-aria="theme">
          <option value="system" data-i18n="themeSystem">System</option>
          <option value="light" data-i18n="themeLight">Light</option>
          <option value="dark" data-i18n="themeDark">Dark</option>
        </select>
      </label>
      <label class="shell-select language-control"><span class="sr-only" data-i18n="language">Language</span>
        <select id="language-select" aria-label="Language" data-i18n-aria="language">
          <option value="en" data-i18n="languageEnglish">English</option>
          <option value="zh" data-i18n="languageChinese">中文</option>
        </select>
      </label>"""

_NAVIGATION = (
    ("/work", "navWork", "Work", '<circle cx="8" cy="8" r="6"/><path d="M8 4v5m0 2v1"/>'),
    ("/board", "navBoard", "Board", '<rect x="2" y="2" width="12" height="12" rx="2"/><path d="M6 2v12M10 2v12"/>'),
    ("/work?bucket=history", "navHistory", "History", '<circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/>'),
    (
        "/timeline",
        "navTimeline",
        "Timeline",
        '<path d="M4 2v12M8 4h6M8 8h6M8 12h6"/><circle cx="4" cy="4" r="1"/>'
        '<circle cx="4" cy="8" r="1"/><circle cx="4" cy="12" r="1"/>',
    ),
    ("/proposals", "navProposals", "Proposals", '<path d="M4 2h5l3 3v9H4zM9 2v3h3M6 8h4M6 11h4"/>'),
    ("/operations", "navOperations", "Operations", '<path d="M1 8h3l2-5 4 10 2-5h3"/>'),
    ("/operations#knowledge", "navKnowledge", "Knowledge", '<path d="M8 3v11M2 2l6 1 6-1v10l-6 1-6-1z"/>'),
)

_NAV_GLYPH = (
    '<svg class="nav-glyph" width="16" height="16" viewBox="0 0 16 16" fill="none" '
    'stroke="currentColor" stroke-width="1.4" aria-hidden="true">'
)

_WORK_LINK_ATTRS = {
    "/work": ' data-nav="work"',
    "/work?bucket=history": ' data-nav="history"',
}
_WORK_COUNTS = {
    "/work": ' <span class="counter" id="nav-work-count">0</span>',
    "/work?bucket=history": ' <span class="counter" id="nav-history-count">0</span>',
}
_OPERATIONS_LINK_ATTRS = {
    "/operations": ' data-primary-view="operations"',
    "/operations#knowledge": ' data-primary-view="knowledge"',
}


def _navigation(page: str, current: str) -> str:
    links = ['<nav class="underline-nav" aria-label="Workbench" data-i18n-aria="primaryNavigation">']
    for href, key, label, glyph in _NAVIGATION:
        selected = " selected" if href == current else ""
        counter = ""
        attrs = ""
        if page == "/work":
            attrs = _WORK_LINK_ATTRS.get(href, "")
            counter = _WORK_COUNTS.get(href, "")
        elif page == "/board" and href == "/board":
            counter = ' <span class="counter attention" id="nav-attention-count" hidden>0</span>'
        elif page == "/operations":
            attrs = _OPERATIONS_LINK_ATTRS.get(href, "")
        active = ' aria-current="page"' if selected else ""
        links.append(
            f'  <a class="underline-nav-item{selected}"{attrs} href="{href}"{active}>'
            f'{_NAV_GLYPH}{glyph}</svg> <span data-i18n="{key}">{label}</span>{counter}</a>'
        )
    links.append("</nav>")
    return "\n".join(links)


def render_shell(html: str, page: str, *, history: bool = False) -> str:
    current = "/work?bucket=history" if history else page
    return (
        html.replace("<!-- workbench-head -->", _HEAD)
        .replace("<!-- workbench-brand -->", _BRAND)
        .replace("<!-- workbench-preferences -->", _PREFERENCES)
        .replace("<!-- workbench-navigation -->", _navigation(page, current))
    )
