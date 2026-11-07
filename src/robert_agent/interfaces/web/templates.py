"""Dashboard HTML view."""

def render_dashboard_html():
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Robert Operations</title>
  <link rel="icon" type="image/svg+xml" href="/assets/robert-favicon.svg">
  <script>
    (function () {
      const savedTheme = localStorage.getItem('robertWorkbenchTheme') || 'system';
      const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
      document.documentElement.dataset.themeChoice = savedTheme;
      document.documentElement.dataset.theme = savedTheme === 'system'
        ? (prefersDark ? 'dark' : 'light')
        : savedTheme;
      document.documentElement.dataset.colorMode = document.documentElement.dataset.theme;
    })();
  </script>
  <style>
    :root {
      color-scheme: light;
      --page: #f6f8fa;
      --surface: #ffffff;
      --surface-raised: #fbfbfc;
      --surface-muted: #f0f2f5;
      --text: #1f2328;
      --muted: #656d76;
      --line: #d8dee4;
      --line-soft: #eaedf0;
      --accent: #0969da;
      --accent-strong: #0550ae;
      --critical: #cf222e;
      --critical-soft: #ffebe9;
      --warning: #9a6700;
      --warning-soft: #fff8c5;
      --info: #0969da;
      --info-soft: #ddf4ff;
      --ok: #1a7f37;
      --ok-soft: #dafbe1;
      --shadow: 0 18px 44px rgba(31, 35, 40, 0.08);
    }
    html[data-theme="dark"] {
      color-scheme: dark;
      --page: #0d1117;
      --surface: #161b22;
      --surface-raised: #1c2128;
      --surface-muted: #21262d;
      --text: #e6edf3;
      --muted: #8b949e;
      --line: #30363d;
      --line-soft: #252b33;
      --accent: #58a6ff;
      --accent-strong: #79c0ff;
      --critical: #ff7b72;
      --critical-soft: #3d1e20;
      --warning: #d29922;
      --warning-soft: #342a14;
      --info: #79c0ff;
      --info-soft: #102a43;
      --ok: #56d364;
      --ok-soft: #14321f;
      --shadow: 0 20px 52px rgba(0, 0, 0, 0.32);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100dvh;
      background: var(--page);
      color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    a { color: inherit; }
    button, input, select, textarea { font: inherit; color: var(--text); }
    button:focus-visible, a:focus-visible, select:focus-visible, input:focus-visible, textarea:focus-visible {
      outline: 3px solid color-mix(in srgb, var(--accent) 45%, transparent);
      outline-offset: 2px;
    }
    .mission-shell {
      display: grid;
      grid-template-columns: 218px minmax(0, 1fr);
      grid-template-rows: auto minmax(0, 1fr);
      min-height: 100dvh;
    }
    .work-filter-bar {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(180px, 280px);
      gap: 8px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line-soft);
      background: var(--surface);
    }
    .work-filter-bar label { display: block; min-width: 0; }
    .work-filter-bar .control-select { width: 100%; min-height: 34px; }
    .search-input {
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-raised);
      color: var(--text);
      padding: 7px 12px;
      box-shadow: inset 0 1px 0 rgba(31, 35, 40, 0.02);
    }
    .search-input::placeholder { color: var(--muted); }
    .control-select, .button, .control-pill {
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface);
      color: var(--text);
      padding: 6px 9px;
      text-decoration: none;
      white-space: nowrap;
      font-size: 12px;
    }
    .button { cursor: pointer; }
    .button.primary { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); background: var(--surface); color: var(--accent-strong); font-weight: 650; }
    html[data-theme="dark"] .button.primary { color: var(--accent-strong); }
    .button:hover, .control-select:hover { border-color: color-mix(in srgb, var(--accent) 50%, var(--line)); }
    .button:active, .nav-button:active, .inline-button:active, .primary-button:active, .danger-button:active { transform: translateY(1px); }
    .refresh-control {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 32px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface);
      padding: 4px 6px;
      white-space: nowrap;
    }
    .refresh-control label { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; }
    .refresh-control select { min-height: 24px; border: 1px solid var(--line); border-radius: 6px; background: var(--surface-raised); padding: 2px 5px; font-size: 12px; }
    .mission-nav {
      position: sticky;
      top: 64px;
      align-self: start;
      height: calc(100dvh - 64px);
      z-index: 19;
      display: grid;
      align-content: start;
      gap: 4px;
      padding: 14px 10px;
      border-right: 1px solid var(--line);
      background: var(--surface);
      overflow-y: auto;
    }
    .nav-button {
      display: flex;
      align-items: center;
      gap: 8px;
      justify-content: flex-start;
      min-height: 34px;
      border: 1px solid transparent;
      border-radius: 7px;
      background: transparent;
      color: var(--muted);
      padding: 7px 10px;
      white-space: nowrap;
      cursor: pointer;
      font-size: 13px;
    }
    .nav-button:hover { color: var(--text); background: var(--surface-muted); }
    .nav-button.active {
      color: var(--text);
      border-color: var(--line);
      background: var(--surface-raised);
      font-weight: 700;
    }
    .nav-icon {
      display: inline-grid;
      place-items: center;
      width: 16px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
    }
    .nav-label { flex: 1; text-align: left; }
    .nav-count {
      min-width: 20px;
      padding: 1px 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface-muted);
      color: var(--text);
      font-size: 11px;
      font-variant-numeric: tabular-nums;
      font-weight: 650;
      text-align: center;
    }
    .nav-button.active .nav-count { border-color: color-mix(in srgb, var(--accent) 32%, var(--line)); color: var(--accent-strong); background: var(--info-soft); }
    .mission-workspace { min-width: 0; padding: 16px 18px 42px; }
    .view-panel[hidden] { display: none; }
    .view-panel { display: grid; gap: 12px; }
    .command-header {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: end;
      padding: 2px 0 4px;
    }
    .command-title h1 { margin: 0; font-size: 22px; line-height: 1.2; letter-spacing: 0; }
    .command-title p { margin: 5px 0 0; color: var(--muted); font-size: 13px; line-height: 1.35; }
    .system-card { display: grid; gap: 10px; align-content: start; }
    .system-card h2, .panel-head h2 { margin: 0; font-size: 13px; line-height: 1.25; }
    .status-stack { display: grid; gap: 8px; }
    .status-stack.list { gap: 0; }
    .status-stack.list .row-between { padding: 10px 12px; border-bottom: 1px solid var(--line-soft); }
    .status-stack.list .row-between:last-child { border-bottom: 0; }
    .health-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      overflow: hidden;
    }
    .health-card {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 6px 10px;
      align-items: center;
      min-height: 56px;
      padding: 10px 12px;
      border-right: 1px solid var(--line-soft);
      background: var(--surface);
    }
    .health-card:last-child { border-right: 0; }
    .health-card strong { justify-self: end; font-size: 18px; line-height: 1; font-variant-numeric: tabular-nums; }
    .health-card span { color: var(--muted); font-size: 12px; }
    .health-card .badge { justify-self: start; }
    .command-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 390px;
      grid-template-areas:
        "inbox inspector"
        "work inspector"
        "activity inspector";
      gap: 12px;
      align-items: start;
    }
    .operator-inbox { grid-area: inbox; }
    .active-workstreams { grid-area: work; }
    .activity-log { grid-area: activity; }
    .command-inspector { grid-area: inspector; position: sticky; top: 102px; }
    .grid-2 { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(340px, 0.9fr); gap: 12px; align-items: start; }
    .work-grid { display: grid; grid-template-columns: minmax(260px, 340px) minmax(260px, 340px) minmax(0, 1fr); gap: 12px; align-items: start; }
    .panel { min-width: 0; overflow: hidden; }
    .panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 1px 0 rgba(31, 35, 40, 0.03);
    }
    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--surface-raised);
    }
    .panel-head span { color: var(--muted); font-size: 12px; }
    .list { display: grid; gap: 0; padding: 0; }
    .row-card, .task-card, .timeline-row, .notification-card, .artifact-row {
      border: 0;
      border-bottom: 1px solid var(--line-soft);
      border-radius: 0;
      background: var(--surface);
      padding: 10px 12px;
      line-height: 1.4;
      min-width: 0;
    }
    .row-card:last-child, .task-card:last-child, .timeline-row:last-child, .notification-card:last-child, .artifact-row:last-child { border-bottom: 0; }
    .task-card { width: 100%; text-align: left; cursor: pointer; transition: border-color 120ms ease, box-shadow 120ms ease, transform 120ms ease; }
    .task-card:hover, .task-card.active { background: var(--surface-raised); box-shadow: inset 3px 0 0 var(--accent); transform: none; }
    .task-select { width: 100%; border: 0; padding: 0; color: inherit; background: transparent; text-align: left; cursor: pointer; }
    .row-between, .row-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
    .item-title { min-width: 0; font-weight: 720; overflow-wrap: anywhere; }
    .issue-row {
      display: grid;
      grid-template-columns: 20px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: start;
    }
    .state-icon {
      display: inline-grid;
      place-items: center;
      width: 16px;
      height: 16px;
      margin-top: 2px;
      border: 1.5px solid var(--muted);
      border-radius: 999px;
      color: var(--muted);
      font-size: 10px;
      line-height: 1;
      font-weight: 800;
    }
    .state-icon.critical { border-color: var(--critical); color: var(--critical); }
    .state-icon.warning { border-color: var(--warning); color: var(--warning); }
    .state-icon.info { border-color: var(--info); color: var(--info); }
    .state-icon.ok { border-color: var(--ok); color: var(--ok); }
    .issue-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }
    .issue-labels {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 6px;
    }
    .work-row {
      display: grid;
      grid-template-columns: minmax(180px, 1.2fr) minmax(130px, 0.8fr) minmax(130px, 0.8fr) auto;
      gap: 12px;
      align-items: center;
    }
    .work-row .action-row { justify-content: flex-end; margin-top: 0; }
    .inbox-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px 12px;
      align-items: start;
    }
    .inbox-row .timeline-meta { grid-column: 1 / -1; }
    .muted { color: var(--muted); }
    .small { font-size: 12px; line-height: 1.4; }
    .empty, .error { padding: 18px; color: var(--muted); line-height: 1.45; }
    .error { color: var(--critical); background: var(--critical-soft); border: 1px solid color-mix(in srgb, var(--critical) 34%, var(--line)); border-radius: 8px; margin: 14px; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 2px 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface-muted);
      color: var(--text);
      font-size: 11px;
      white-space: nowrap;
      max-width: 100%;
    }
    .badge.critical { border-color: color-mix(in srgb, var(--critical) 34%, var(--line)); background: var(--critical-soft); color: var(--critical); }
    .badge.warning { border-color: color-mix(in srgb, var(--warning) 34%, var(--line)); background: var(--warning-soft); color: var(--warning); }
    .badge.info { border-color: color-mix(in srgb, var(--info) 30%, var(--line)); background: var(--info-soft); color: var(--info); }
    .badge.ok { border-color: color-mix(in srgb, var(--ok) 30%, var(--line)); background: var(--ok-soft); color: var(--ok); }
    .row-card.critical, .notification-card.critical, .timeline-row.critical { box-shadow: inset 3px 0 0 var(--critical); }
    .row-card.warning, .notification-card.warning, .timeline-row.warning { box-shadow: inset 3px 0 0 var(--warning); }
    .row-card.info, .notification-card.info, .timeline-row.info { box-shadow: inset 3px 0 0 var(--info); }
    .row-card.ok, .notification-card.ok, .timeline-row.ok { box-shadow: inset 3px 0 0 var(--ok); }
    .external-link { color: var(--accent-strong); text-decoration-thickness: 1px; text-underline-offset: 2px; overflow-wrap: anywhere; }
    .meta-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; padding: 12px; }
    .field { min-width: 0; border: 1px solid var(--line-soft); border-radius: 7px; padding: 8px; background: var(--surface-raised); }
    .field label { display: block; margin-bottom: 5px; color: var(--muted); font-size: 12px; }
    .field div, .field a { overflow-wrap: anywhere; font-size: 13px; line-height: 1.4; }
    .detail-title { padding: 12px 12px 0; }
    .detail-title h3 { margin: 0; font-size: 17px; line-height: 1.2; overflow-wrap: anywhere; }
    .detail-title p { margin: 6px 0 0; color: var(--muted); }
    .detail-actions { display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 12px 0; }
    .subhead { margin: 14px 12px 7px; color: var(--muted); font-size: 12px; font-weight: 700; }
    .json-chip { display: inline-flex; margin: 4px 6px 0 0; padding: 4px 7px; border: 1px solid var(--line); border-radius: 999px; background: var(--surface-muted); color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .metadata { margin-top: 8px; }
    .metadata summary { cursor: pointer; color: var(--muted); font-size: 12px; }
    pre {
      margin: 8px 0 0;
      max-height: 260px;
      overflow: auto;
      border-radius: 7px;
      border: 1px solid var(--line);
      background: #111827;
      color: #e5e7eb;
      padding: 10px;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    html[data-theme="dark"] pre { background: #0b1220; color: #dbeafe; }
    .artifact-preview {
      min-height: 100%;
      overflow: auto;
      border-radius: 7px;
      border: 1px solid #d0d7de;
      background: #ffffff;
      color: #24292f;
      padding: 16px;
      font-size: 14px;
      line-height: 1.55;
    }
    .artifact-preview.placeholder { color: #57606a; }
    .artifact-preview.error { color: #cf222e; background: #ffebe9; }
    .preview-scrim {
      position: fixed;
      inset: 0;
      z-index: 50;
      display: none;
      background: rgba(15, 23, 42, 0.38);
    }
    .preview-scrim.open { display: block; }
    .artifact-drawer {
      position: fixed;
      top: 0;
      right: 0;
      bottom: 0;
      z-index: 60;
      display: flex;
      width: min(760px, calc(100vw - 32px));
      max-width: 100vw;
      flex-direction: column;
      border-left: 1px solid var(--line);
      background: var(--surface);
      box-shadow: -22px 0 48px rgba(15, 23, 42, 0.18);
      transform: translateX(100%);
    }
    .artifact-drawer.open { transform: translateX(0); }
    .artifact-drawer-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--surface-raised);
    }
    .artifact-drawer-head h2 {
      margin: 0;
      min-width: 0;
      font-size: 15px;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }
    .artifact-drawer-body {
      min-height: 0;
      flex: 1;
      overflow: auto;
      padding: 14px;
    }
    body.preview-open { overflow: hidden; }
    .artifact-preview-header {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 14px;
      padding-bottom: 12px;
      border-bottom: 1px solid #d8dee4;
      color: #57606a;
      font-size: 12px;
    }
    .artifact-preview-header span {
      max-width: 100%;
      padding: 2px 7px;
      border: 1px solid #d0d7de;
      border-radius: 999px;
      background: #f6f8fa;
      overflow-wrap: anywhere;
    }
    .artifact-preview h1, .artifact-preview h2, .artifact-preview h3 {
      margin: 18px 0 10px;
      color: #24292f;
      line-height: 1.25;
      border-bottom: 1px solid #d8dee4;
      padding-bottom: 0.3em;
    }
    .artifact-preview h1:first-child, .artifact-preview h2:first-child, .artifact-preview h3:first-child { margin-top: 0; }
    .artifact-preview p, .artifact-preview ul, .artifact-preview blockquote { margin: 0 0 12px; }
    .artifact-preview ul { padding-left: 24px; }
    .artifact-preview blockquote {
      padding-left: 12px;
      border-left: 4px solid #d0d7de;
      color: #57606a;
    }
    .artifact-preview code {
      font-family: ui-monospace, SFMono-Regular, SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
      border-radius: 6px;
      background: rgba(175, 184, 193, 0.2);
      padding: 0.2em 0.4em;
      font-size: 85%;
    }
    .artifact-preview pre.gh-code {
      max-height: none;
      margin: 10px 0 14px;
      border: 1px solid #d0d7de;
      background: #f6f8fa;
      color: #24292f;
      padding: 16px;
      font-size: 12px;
      line-height: 1.45;
      white-space: pre;
      overflow: auto;
    }
    .artifact-preview pre.gh-code code {
      background: transparent;
      padding: 0;
      border-radius: 0;
      font-size: inherit;
    }
    .json-key { color: #0550ae; }
    .json-string { color: #0a3069; }
    .json-number { color: #953800; }
    .json-literal { color: #cf222e; }
    .inline-button, .primary-button, .danger-button { min-height: 30px; border: 1px solid var(--line); border-radius: 7px; background: var(--surface-raised); color: var(--text); padding: 5px 9px; cursor: pointer; font-size: 12px; }
    .primary-button { border-color: color-mix(in srgb, var(--accent) 45%, var(--line)); background: var(--surface); color: var(--accent-strong); font-weight: 650; }
    html[data-theme="dark"] .primary-button { color: var(--accent-strong); }
    .danger-button { border-color: color-mix(in srgb, var(--critical) 45%, var(--line)); background: var(--critical-soft); color: var(--critical); }
    .inline-button:hover, .primary-button:hover, .danger-button:hover { border-color: var(--accent); }
    .artifact-actions, .action-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 8px; }
    .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
    .form-grid label, .proposal-box label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
    .form-grid input, .form-grid select, .form-grid textarea, .proposal-box select { width: 100%; min-height: 32px; border: 1px solid var(--line); border-radius: 7px; background: var(--surface); color: var(--text); padding: 6px 8px; }
    .form-wide { grid-column: 1 / -1; }
    .proposal-box { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(220px, 320px) auto; gap: 10px; align-items: end; margin: 12px; padding: 12px; border-bottom: 1px solid var(--line-soft); background: var(--surface-raised); }
    .boundary-note { margin: 14px; padding: 12px; border: 1px solid color-mix(in srgb, var(--warning) 35%, var(--line)); border-radius: 8px; background: var(--warning-soft); color: var(--warning); line-height: 1.45; }
    .timeline-row { display: grid; gap: 8px; }
    .timeline-meta { display: flex; flex-wrap: wrap; gap: 8px; color: var(--muted); font-size: 12px; }
    .timeline-filter-bar {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line-soft);
      background: var(--surface);
    }
    .checkbox-pill {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      min-height: 28px;
      padding: 4px 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface-raised);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    @media (prefers-reduced-motion: no-preference) {
      .task-card, .button, .nav-button, .inline-button, .primary-button, .danger-button { transition: border-color 140ms ease, background 140ms ease, color 140ms ease, transform 140ms ease, box-shadow 140ms ease; }
      .artifact-drawer { transition: transform 180ms ease; }
    }
    @media (max-width: 1120px) {
      .mission-shell { grid-template-columns: 1fr; grid-template-rows: auto auto minmax(0, 1fr); }
      .mission-nav {
        position: sticky;
        top: 64px;
        height: auto;
        display: flex;
        border-right: 0;
        border-bottom: 1px solid var(--line);
        overflow-x: auto;
      }
      .mission-workspace { grid-column: 1; }
      .command-grid { grid-template-columns: 1fr; grid-template-areas: "inbox" "work" "inspector" "activity"; }
      .command-inspector { position: static; }
      .grid-2, .work-grid { grid-template-columns: 1fr; }
      .health-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 760px) {
      .work-filter-bar { grid-template-columns: 1fr; }
      .mission-nav { top: 0; }
      .mission-workspace { padding: 12px 10px 32px; }
      .command-header { display: grid; align-items: start; }
      .command-title h1 { font-size: 20px; }
      .health-grid, .meta-grid { grid-template-columns: 1fr; }
      .health-card { border-right: 0; border-bottom: 1px solid var(--line-soft); }
      .health-card:last-child { border-bottom: 0; }
      .work-row, .inbox-row { grid-template-columns: 1fr; }
      .issue-row { grid-template-columns: 20px minmax(0, 1fr); }
      .issue-labels { grid-column: 2; justify-content: flex-start; }
      .work-row .action-row { justify-content: flex-start; }
      .row-between, .row-top { flex-direction: column; }
      .form-grid, .proposal-box { grid-template-columns: 1fr; }
      .checkbox-pill { white-space: normal; }
      .control-select, .button, .control-pill, .refresh-control { width: 100%; justify-content: center; }
      .refresh-control { grid-column: 1 / -1; }
      .badge { white-space: normal; }
    }
  </style>
  <link rel="stylesheet" href="/assets/github-shell.css">
  <link rel="stylesheet" href="/assets/operations.css">
</head>
<body>
  <main id="dashboard-app" class="mission-shell" data-state="loading">
    <header class="global-bar">
      <a class="brand" href="/" aria-label="Robert Workbench">
        <span class="brand-mark" aria-hidden="true"><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="6" cy="5" r="2.5"/><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="6" r="2.5"/><path d="M6 7.5v9M18 8.5v2a6 6 0 0 1-6 6H6"/></svg></span>
        <span>Robert</span>
      </a>
      <div class="global-actions" aria-label="Dashboard controls" data-i18n-aria="dashboardControls">
        <span class="live-indicator"><i aria-hidden="true"></i><span class="responsive-label" id="last-updated" data-i18n="loading">Loading</span></span>
        <button class="shell-button" type="button" id="refresh-button" data-i18n="refresh">Refresh</button>
        <details class="shell-settings">
          <summary class="shell-button" data-i18n="settings">Settings</summary>
          <div class="shell-settings-panel">
            <label><span data-i18n="theme">Theme</span><select id="theme-select" aria-label="Theme" data-i18n-aria="theme">
              <option value="system" data-i18n="themeSystem">System</option>
              <option value="light" data-i18n="themeLight">Light</option>
              <option value="dark" data-i18n="themeDark">Dark</option>
            </select></label>
            <label><span data-i18n="language">Language</span><select id="language-select" aria-label="Language" data-i18n-aria="language">
              <option value="en" data-i18n="languageEnglish">English</option>
              <option value="zh" data-i18n="languageChinese">中文</option>
            </select></label>
            <span class="refresh-control">
              <label><input type="checkbox" id="auto-refresh-toggle"> <span data-i18n="autoRefresh">Auto refresh</span></label>
              <select id="refresh-interval" aria-label="Auto refresh interval">
                <option value="15000">15s</option>
                <option value="30000" selected>30s</option>
                <option value="60000">60s</option>
              </select>
              <span class="muted small" id="refresh-status" data-i18n="off">Off</span>
            </span>
            <a class="button" href="/data.json" target="_blank" rel="noopener noreferrer" data-i18n="viewJson">View JSON</a>
          </div>
        </details>
      </div>
    </header>
    <header class="repo-header">
      <div class="repo-context"><a href="/">Robert</a><span>/</span><strong id="repo-context">operations</strong></div>
      <nav class="underline-nav" aria-label="Workbench" data-i18n-aria="primaryNavigation">
        <a class="underline-nav-item" href="/work"><svg class="nav-glyph" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 4v5m0 2v1"/></svg> <span data-i18n="primaryWork">Work</span></a>
        <a class="underline-nav-item" href="/board"><svg class="nav-glyph" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><rect x="2" y="2" width="12" height="12" rx="2"/><path d="M6 2v12M10 2v12"/></svg> <span data-i18n="navBoard">Board</span></a>
        <a class="underline-nav-item" href="/work?bucket=history"><svg class="nav-glyph" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/></svg> <span data-i18n="navHistory">History</span></a>
        <a class="underline-nav-item selected" data-primary-view="operations" href="/operations"><svg class="nav-glyph" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M1 8h3l2-5 4 10 2-5h3"/></svg> <span data-i18n="navOperations">Operations</span></a>
        <a class="underline-nav-item" data-primary-view="knowledge" href="/operations#knowledge"><svg class="nav-glyph" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" aria-hidden="true"><path d="M8 3v11M2 2l6 1 6-1v10l-6 1-6-1z"/></svg> <span data-i18n="navKnowledge">Knowledge</span></a>
      </nav>
    </header>
    <nav class="mission-nav" aria-label="Dashboard sections" data-i18n-aria="diagnosticNavigation">
      <button class="nav-button active" type="button" data-view="command"><span class="nav-icon">#</span><span class="nav-label" data-i18n="navCommand">Command</span><span class="nav-count" id="nav-count-command">0</span></button>
      <button class="nav-button" type="button" data-view="timeline"><span class="nav-icon">T</span><span class="nav-label" data-i18n="navTimeline">Timeline</span><span class="nav-count" id="nav-count-timeline">0</span></button>
      <button class="nav-button" type="button" data-view="notifications"><span class="nav-icon">!</span><span class="nav-label" data-i18n="navNotifications">Notifications</span><span class="nav-count" id="nav-count-notifications">0</span></button>
      <button class="nav-button" type="button" data-view="daemon"><span class="nav-icon">R</span><span class="nav-label" data-i18n="navDaemon">Daemon</span><span class="nav-count" id="nav-count-daemon">0</span></button>
    </nav>
    <section class="mission-workspace">
      <section class="view-panel" id="view-command" data-view-panel="command">
        <section class="command-header">
          <div class="command-title">
            <h1 data-i18n="commandHeadline">Robert Command Center</h1>
            <p id="dashboard-subtitle" data-i18n="loadingState">Loading local control-plane state.</p>
          </div>
          <span id="system-snapshot-status" class="badge info" data-i18n="loading">Loading</span>
        </section>
        <section class="health-grid" id="summary-metrics" aria-label="Summary"></section>
        <section class="command-grid">
          <article class="panel operator-inbox">
            <div class="panel-head"><h2 data-i18n="operatorNextSteps">Operator next steps</h2><span id="next-step-count"></span></div>
            <div class="list" id="operator-next-steps"></div>
          </article>
          <article class="panel active-workstreams">
            <div class="panel-head"><h2 data-i18n="currentWork">Current work</h2><span id="overview-task-count"></span></div>
            <div class="list" id="now-lane"></div>
          </article>
          <aside class="panel command-inspector">
            <div class="panel-head"><h2 data-i18n="commandInspector">Inspector</h2><span id="command-inspector-status"></span></div>
            <div id="command-inspector"></div>
            <div class="panel-head"><h2 data-i18n="systemSnapshot">System snapshot</h2><span></span></div>
            <div class="status-stack list" id="system-snapshot"></div>
          </aside>
          <article class="panel activity-log">
            <div class="panel-head"><h2 data-i18n="recentActivity">Recent activity</h2><span id="activity-count"></span></div>
            <div class="list" id="command-activity"></div>
          </article>
        </section>
      </section>
      <section class="view-panel" id="view-work" data-view-panel="work" hidden>
        <section class="work-grid">
          <article class="panel">
            <div class="panel-head"><h2 data-i18n="workItems">PR / Issue</h2><span id="task-count"></span></div>
            <div class="work-filter-bar">
              <label><span class="sr-only" data-i18n="workSearch">Search work</span><input class="search-input" id="work-search" type="search" data-i18n-placeholder="workSearchPlaceholder" placeholder="Search PR title or task ID" aria-label="Search work"></label>
              <label><span class="sr-only" data-i18n="workProject">Project</span><select class="control-select" id="work-repo-filter" aria-label="Project"></select></label>
            </div>
            <div class="list" id="task-list"></div>
          </article>
          <aside class="panel" aria-live="polite">
            <div class="panel-head"><h2 data-i18n="workTasks">Tasks</h2><span id="work-task-count"></span></div>
            <div class="list" id="work-task-list"></div>
          </aside>
          <aside class="panel" id="detail-drawer" aria-live="polite">
            <div class="panel-head"><h2 data-i18n="taskDetail">Task detail</h2><span id="detail-status"></span></div>
            <div id="task-detail"></div>
            <div class="panel-head"><h2 data-i18n="logsArtifacts">Logs and artifacts</h2><span id="artifact-count"></span></div>
            <div class="list" id="artifact-list"></div>
          </aside>
        </section>
      </section>
      <section class="view-panel" id="view-daemon" data-view-panel="daemon" hidden>
        <section class="grid-2">
          <article class="panel">
            <div class="panel-head"><h2 data-i18n="daemonHealth">Daemon health</h2><span id="daemon-run-status"></span></div>
            <div class="meta-grid" id="daemon-health"></div>
          </article>
          <article class="panel">
            <div class="panel-head"><h2 data-i18n="daemonEvents">Daemon events</h2><span id="daemon-event-count"></span></div>
            <div class="list" id="daemon-events"></div>
          </article>
        </section>
      </section>
      <section class="view-panel" id="view-timeline" data-view-panel="timeline" hidden>
        <article class="panel">
          <div class="panel-head"><h2 data-i18n="timeline">Timeline</h2><span id="timeline-count"></span></div>
          <div class="timeline-filter-bar" id="timeline-filters" aria-label="Timeline type filters"></div>
          <div class="list" id="timeline-list"></div>
        </article>
      </section>
      <section class="view-panel" id="view-knowledge" data-view-panel="knowledge" hidden>
        <section class="grid-2">
          <article class="panel">
            <div class="panel-head"><h2 data-i18n="knowledgeReview">Knowledge review</h2><span id="knowledge-count"></span></div>
            <div class="boundary-note" data-i18n="knowledgeBoundary">Local approval only. This page writes only to local runtime knowledge tables.</div>
            <form class="proposal-box" id="knowledge-propose-form">
              <div><strong data-i18n="generateCandidates">Generate candidates</strong><div class="muted small" data-i18n="generateCandidatesHelp">Read local project memory and create pending candidates for human review.</div></div>
              <label><span data-i18n="repository">Repository</span><select name="repo_id" id="knowledge-propose-repo" required></select></label>
              <button class="primary-button" type="submit" data-i18n="generate">Generate</button>
            </form>
            <div class="list" id="knowledge-candidates"></div>
          </article>
          <article class="panel">
            <div class="panel-head"><h2 data-i18n="activeKnowledge">Active runtime knowledge</h2><span id="runtime-knowledge-count"></span></div>
            <div class="list" id="runtime-knowledge-list"></div>
          </article>
        </section>
      </section>
      <section class="view-panel" id="view-notifications" data-view-panel="notifications" hidden>
        <article class="panel">
          <div class="panel-head"><h2 data-i18n="notificationCenter">Notification center</h2><span id="notification-count"></span></div>
          <label class="notification-filter"><span data-i18n="severityFilter">Severity</span>
            <select id="notification-severity"><option value="" data-i18n="allSeverities">All severities</option><option value="attention" data-i18n="attentionOnly">Critical and warning</option><option value="critical" data-dynamic-label="critical">Critical</option><option value="warning" data-dynamic-label="warning">Warning</option><option value="info" data-dynamic-label="info">Info</option><option value="ok" data-dynamic-label="ok">OK</option></select>
          </label>
          <p class="muted" data-i18n="notificationScope">Filters apply to recently loaded notifications; original time order is preserved.</p>
          <div class="list" id="notification-center"></div>
        </article>
      </section>
    </section>
  </main>
  <div id="preview-scrim" class="preview-scrim" data-close-artifact-preview></div>
  <aside id="artifact-preview-drawer" class="artifact-drawer" aria-hidden="true" aria-labelledby="artifact-preview-title">
    <div class="artifact-drawer-head">
      <h2 id="artifact-preview-title" data-i18n="artifactPreview">Artifact preview</h2>
      <button id="artifact-preview-close" class="inline-button" type="button" data-close-artifact-preview data-i18n="closePreview">Close</button>
    </div>
    <div class="artifact-drawer-body">
      <div id="artifact-preview" class="artifact-preview placeholder" data-i18n="selectArtifact">Select an artifact preview.</div>
    </div>
  </aside>
  <script>
    const translations = {
      en: {
        navWorkbench: 'Workbench', primaryWork: 'Work', navBoard: 'Board', navHistory: 'History', navOperations: 'Operations', navKnowledge: 'Knowledge',
        primaryNavigation: 'Primary navigation', diagnosticNavigation: 'Operations sections', repositoryContext: 'Repository context', dashboardControls: 'Dashboard controls',
        productSubtitle: 'Your Repo Teammate', loadingState: 'Loading local control-plane state.', theme: 'Theme', themeSystem: 'System', themeLight: 'Light', themeDark: 'Dark', language: 'Language', languageEnglish: 'English', languageChinese: 'Chinese', loading: 'Loading', autoRefresh: 'Auto refresh', off: 'Off', refresh: 'Refresh', settings: 'Settings', viewJson: 'View JSON', navCommand: 'Command', navWork: 'Work', navDaemon: 'Daemon', navTimeline: 'Timeline', navKnowledge: 'Knowledge', navNotifications: 'Notifications', commandHeadline: 'Robert Command Center', commandLead: 'Runtime, queue, publication and recent activity.', commandInspector: 'Runtime inspector', systemSnapshot: 'System snapshot', operatorNextSteps: 'Operator Inbox', currentWork: 'Recent workflows', recentActivity: 'Event stream', tasks: 'Tasks', workItems: 'PR / Issue', workTasks: 'Tasks in source', taskDetail: 'Task detail', logsArtifacts: 'Logs and artifacts', daemonHealth: 'Daemon health', daemonEvents: 'Daemon events', timeline: 'Timeline', timelineFilters: 'Timeline filters', knowledgeReview: 'Knowledge review', knowledgeBoundary: 'Local approval only. This page writes only to local runtime knowledge tables.', generateCandidates: 'Generate candidates', generateCandidatesHelp: 'Read local project memory and create pending candidates for human review.', repository: 'Repository', generate: 'Generate', activeKnowledge: 'Active runtime knowledge', notificationCenter: 'Notification center', artifactPreview: 'Artifact preview', closePreview: 'Close', selectArtifact: 'Select an artifact preview.', updated: 'Updated', every: 'Every', open: 'open', recent: 'recent', pending: 'pending', active: 'active', inspect: 'Inspect', preview: 'Preview', openGithub: 'Open GitHub', metadata: 'Metadata', noOperatorAction: 'No operator action is waiting.', noWorkItems: 'No PR or issue work recorded.', noTasks: 'No tasks recorded.', noActivity: 'No recent activity recorded.', noDaemon: 'No daemon run recorded.', noDaemonEvents: 'No daemon events recorded.', noTimeline: 'No timeline evidence recorded.', noNotifications: 'No notifications recorded.', noKnowledgeTables: 'Knowledge tables are not available in this database yet.', noPendingKnowledge: 'No pending knowledge candidates.', noActiveKnowledge: 'No active runtime knowledge.', noArtifacts: 'No artifacts for this task.', artifactLoading: 'Loading artifact preview.', artifactUnavailable: 'Preview unavailable', failedLoad: 'Failed to load dashboard data', approve: 'Approve', reject: 'Reject', scopeType: 'Scope type', scopeValue: 'Scope value', approvedBy: 'Approved by', reviewer: 'Reviewer', rejectReason: 'Reject reason', allowedActions: 'Allowed GitHub actions', recommendedSkills: 'Recommended skills', latestNotification: 'Latest notification', source: 'Source', daemon: 'Daemon', queue: 'Queue', attention: 'Attention', publish: 'Publish', latestRun: 'Total runs (all time)', idleHealthy: 'Idle and healthy', needsReview: 'Needs review', workSearch: 'Search work', workProject: 'Project', workSearchPlaceholder: 'Search PR title or task ID', allProjects: 'All projects', severityFilter: 'Severity', allSeverities: 'All severities', attentionOnly: 'Critical and warning', noMatchingNotifications: 'No notifications match this filter', notificationScope: 'Filters apply to recently loaded notifications; original time order is preserved.'
      },
      zh: {
        navWorkbench: '工作台', primaryWork: '工作', navBoard: '任务看板', navHistory: '历史', navOperations: '运行状态', navKnowledge: '知识',
        primaryNavigation: '一级导航', diagnosticNavigation: '运行状态分区', repositoryContext: '仓库上下文', dashboardControls: '控制台控制',
        productSubtitle: 'Your Repo Teammate', loadingState: '正在读取本地控制面状态。', theme: '主题', themeSystem: '跟随系统', themeLight: '浅色', themeDark: '深色', language: '语言', languageEnglish: 'English', languageChinese: '中文', loading: '加载中', autoRefresh: '自动刷新', off: '关闭', refresh: '刷新', settings: '设置', viewJson: '查看 JSON', navCommand: '指挥台', navWork: '工作流', navDaemon: '守护进程', navTimeline: '时间线', navKnowledge: '知识', navNotifications: '通知中心', commandHeadline: 'Robert 指挥中心', commandLead: '查看运行状态、调度队列、结果发布与最近事件。', commandInspector: '运行检查器', systemSnapshot: '系统快照', operatorNextSteps: '操作收件箱', currentWork: '最近工作流', recentActivity: '事件流', tasks: '任务', workItems: 'PR / Issue', workTasks: '来源下的任务', taskDetail: '任务详情', logsArtifacts: '日志和产物', daemonHealth: '守护进程健康度', daemonEvents: '守护进程事件', timeline: '时间线', timelineFilters: '时间线过滤', knowledgeReview: '知识审核', knowledgeBoundary: '仅本地审批。此页面只写入本地 runtime knowledge 表。', generateCandidates: '生成候选知识', generateCandidatesHelp: '读取本地项目记忆，生成待人工审核的候选项。', repository: '仓库', generate: '生成', activeKnowledge: '已生效知识', notificationCenter: '通知中心', artifactPreview: '产物预览', closePreview: '关闭', selectArtifact: '选择一个产物预览。', updated: '已更新', every: '每', open: '未处理', recent: '最近', pending: '待处理', active: '活跃', inspect: '查看', preview: '预览', openGithub: '打开 GitHub', metadata: '元数据', noOperatorAction: '暂无需要操作的事项。', noWorkItems: '暂无 PR 或 Issue 工作记录。', noTasks: '暂无任务记录。', noActivity: '暂无最近动态。', noDaemon: '暂无守护进程运行记录。', noDaemonEvents: '暂无守护进程事件。', noTimeline: '暂无时间线证据。', noNotifications: '暂无通知。', noKnowledgeTables: '当前数据库还没有 knowledge 表。', noPendingKnowledge: '暂无待审核知识。', noActiveKnowledge: '暂无已生效知识。', noArtifacts: '这个任务暂无产物。', artifactLoading: '正在加载产物预览。', artifactUnavailable: '无法预览', failedLoad: '加载 dashboard 数据失败', approve: '批准', reject: '拒绝', scopeType: '作用域类型', scopeValue: '作用域值', approvedBy: '批准人', reviewer: '审核人', rejectReason: '拒绝原因', allowedActions: '允许的 GitHub 动作', recommendedSkills: '推荐技能', latestNotification: '最近通知', source: '来源', daemon: '守护', queue: '队列', attention: '关注', publish: '发布', latestRun: '累计运行次数', idleHealthy: '空闲且健康', needsReview: '需要查看', workSearch: '搜索工作流', workProject: '项目', workSearchPlaceholder: '搜索 PR 标题或 task ID', allProjects: '全部项目', severityFilter: '严重程度', allSeverities: '全部级别', attentionOnly: '严重和警告', noMatchingNotifications: '没有符合筛选条件的通知', notificationScope: '筛选范围为最近加载的通知，保留原有时间顺序。'
      }
    };
    const state = { data: null, selectedTaskId: null, selectedWorkItemId: null, currentView: 'command', autoRefreshTimer: null, autoRefreshIntervalMs: 30000, language: 'en', theme: 'system', searchQuery: '', workSearchQuery: '', workRepoFilter: '', notificationSeverity: '', timelineHiddenTypes: new Set(['daemon_heartbeat']) };
    const severityByState = { needs_attention: 'critical', worker_stale: 'warning', worker_retrying: 'warning', result_pending_audit: 'warning', waiting_publish: 'info', waiting_user: 'info', worker_running: 'info', waiting_worker: 'info', completed: 'ok', ignored: 'ok' };
    const labelByState = { needs_attention: 'Needs attention', worker_stale: 'Worker stale', worker_retrying: 'Worker retrying', result_pending_audit: 'Result pending audit', waiting_publish: 'Waiting publish', waiting_user: 'Waiting user', worker_running: 'Worker running', waiting_worker: 'Waiting worker', completed: 'Completed', ignored: 'Ignored', unknown: 'Unknown' };
    const dynamicLabels = {
      en: {},
      zh: {
        Actions: '动作', Attempt: '尝试', Channel: '渠道', Config: '配置', Created: '创建时间', Expires: '过期时间', Finished: '结束时间', Heartbeat: '心跳', Kind: '类型', Lifecycle: '生命周期', 'Latest attempt': '最近尝试', 'Latest event': '最近事件', 'Lease key': '租约键', 'Lease owner': '租约持有者', 'Lease status': '租约状态', 'Next action': '下一步动作', Owner: '持有者', Route: '路由', 'Route confidence': '路由置信度', 'Run ID': '运行 ID', 'Run status': '运行状态', Severity: '严重级别', Source: '来源', 'Source key': '来源标识', Started: '开始时间', Status: '状态', Trigger: '触发事件', Type: '类型', 'Type and number': '类型和编号', Updated: '更新时间', Workstream: '工作流',
        repos: '仓库', repo: '仓库', workstreams: '工作流', tasks: '任务', events: '事件', notifications: '通知', wakeups: '唤醒', files: '文件', bytes: '字节', 'size unknown': '大小未知', '0 files': '0 个文件', 'No repositories': '暂无仓库', memories: '条记忆', 'local control plane': '本地控制面',
        diagnostics_pruned: '诊断记录清理完成', local_drain_completed: '本轮本地队列处理完成',
        local_drain_started: '开始处理本地队列', live_poll_started: '开始同步 GitHub', live_poll_completed: 'GitHub 同步完成', live_poll_failed: 'GitHub 同步失败', daemon_heartbeat: '守护进程心跳', pending_authorization: '等待授权', resolve_authorization_lookup: '确认授权信息',
        critical: '严重', warning: '警告', info: '信息', ok: '正常', unknown: '未知', active: '活跃', running: '运行中', completed: '已完成', failed: '失败', canceled: '已取消', expired: '已过期', pending: '待处理', skipped: '已跳过', recorded: '已记录', healthy: '健康',
        notification: '通知', run: '运行', daemon_event: '守护事件', github_action: 'GitHub 动作', github_event: 'GitHub 事件', worker_result: 'Worker 结果', next_step: '下一步', event: '事件',
        'run failed': '运行失败', failed_dispatch: '分发失败', worker_dispatch_failed: 'Worker 分发失败', worker_startup_failed: 'Worker 启动失败', worker_process_exited: 'Worker 进程退出', publish_failed: '发布失败', lease_expired: '租约已过期',
        needs_attention: '需要处理', worker_stale: 'Worker 已过期', worker_retrying: 'Worker 重试中', result_pending_audit: '结果待审计', waiting_publish: '等待发布', waiting_user: '等待用户', worker_running: 'Worker 运行中', waiting_worker: '等待 Worker', ignored: '已忽略', unrouted: '未路由', route: '路由', path: '路径', symbol: '符号', workstream: '工作流', global: '全局',
        inspect_recent_events: '查看最近事件', inspect_attention_tasks: '查看需处理任务', inspect_worker_timeout: '查看 Worker 超时', inspect_worker_startup: '查看 Worker 启动', inspect_failure_notification: '查看失败通知', inspect_github_publish_failure: '查看 GitHub 发布失败', inspect_github_publish_skipped: '查看跳过的 GitHub 发布', publish_accepted_github_actions: '发布已接受的 GitHub 动作', wait_for_trusted_user_reply: '等待可信用户回复', inspect_worker_heartbeat: '查看 Worker 心跳', inspect_worker_resume: '查看 Worker 恢复', wait_next_cycle_or_run_once: '等待下一轮或运行 run_once', wait_for_worker_completion: '等待 Worker 完成', wait_for_worker_or_supervise: '等待或监督 Worker', inspect_task: '查看任务'
      }
    };
    function $(id) { return document.getElementById(id); }
    function t(key, vars = {}) { const dict = translations[state.language] || translations.en; let value = dict[key] || translations.en[key] || key; Object.entries(vars).forEach(([name, replacement]) => { value = value.replaceAll(`{${name}}`, String(replacement)); }); return value; }
    function esc(value) { return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;'); }
    function dl(value) { const text = String(value ?? ''); return (dynamicLabels[state.language] || {})[text] || text; }
    function stateLabel(value) { return state.language === 'zh' ? dl(value || 'unknown') : (labelByState[value] || dl(value || 'unknown')); }
    function matchesSearchObject(item) { const query = state.searchQuery.trim().toLowerCase(); if (!query) return true; const text = JSON.stringify(item || {}).toLowerCase(); return query.split(/\s+/).every((token) => { const match = token.match(/^([a-z_]+):(.+)$/); if (!match) return text.includes(token); return text.includes(match[2]); }); }
    function repoContext(data) { const repos = data.repos || []; if (repos.length === 1) return repos[0].full_name || dl('local control plane'); if (repos.length > 1) return `${repos.length} ${dl('repos')}`; return dl('local control plane'); }
    function setText(id, value) { const node = $(id); if (node) node.textContent = value; }
    function renderNavCounts(data) { const notifications = data.notification_center || []; const daemonEvents = (data.daemon || {}).recent_events || []; setText('nav-count-command', notifications.filter((item) => item.severity === 'critical' || item.severity === 'warning').length); setText('nav-count-daemon', daemonEvents.length); setText('nav-count-timeline', filteredTimelineItems(data).length); setText('nav-count-notifications', notifications.length); }
    function pad2(value) { return String(value).padStart(2, '0'); }
    function fmt(value) { if (!value) return '-'; const date = new Date(value); if (Number.isNaN(date.getTime())) return String(value); return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`; }
    function fmtShort(value) { if (!value) return '-'; const date = new Date(value); if (Number.isNaN(date.getTime())) return String(value); return `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`; }
    function jsonBlock(value) { if (!value || (typeof value === 'object' && Object.keys(value).length === 0)) return ''; return `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`; }
    function metadataDetails(value) { const block = jsonBlock(value); if (!block) return ''; return `<details class="metadata"><summary>${esc(t('metadata'))}</summary>${block}</details>`; }
    function badge(label, tone) { return `<span class="badge ${tone || ''}">${esc(dl(label))}</span>`; }
    function externalLink(url, label) { if (!/^https?:\/\//i.test(url || '')) return '-'; return `<a class="external-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label || url)}</a>`; }
    function sourceTitle(item) { return item?.source_title || item?.title || item?.source_key || item?.id || item?.task_id || '-'; }
    function sourceMeta(item) { const parts = []; if (item?.source_key) parts.push(item.source_key); else if (item?.workstream_id) parts.push(item.workstream_id); if (item?.source_type || item?.source_number) parts.push(`${item.source_type || 'source'} #${item.source_number || '-'}`); return parts.filter(Boolean).join(' / '); }
    function applyTranslations() { document.documentElement.lang = state.language === 'zh' ? 'zh-CN' : 'en'; document.querySelectorAll('[data-i18n]').forEach((node) => { node.textContent = t(node.getAttribute('data-i18n')); }); document.querySelectorAll('[data-i18n-placeholder]').forEach((node) => { node.setAttribute('placeholder', t(node.getAttribute('data-i18n-placeholder'))); }); document.querySelectorAll('[data-i18n-aria]').forEach((node) => { node.setAttribute('aria-label', t(node.getAttribute('data-i18n-aria'))); }); $('theme-select').value = state.theme; $('language-select').value = state.language; }
    function setTheme(value) { const next = ['system', 'light', 'dark'].includes(value) ? value : 'system'; state.theme = next; window.localStorage.setItem('robertWorkbenchTheme', next); document.documentElement.dataset.themeChoice = next; const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches; document.documentElement.dataset.theme = next === 'system' ? (prefersDark ? 'dark' : 'light') : next; document.documentElement.dataset.colorMode = document.documentElement.dataset.theme; $('theme-select').value = next; }
    function setLanguage(value) { const next = translations[value] ? value : 'en'; state.language = next; window.localStorage.setItem('robertLanguage', next); applyTranslations(); renderAll(); }
    function applyPreferences() { state.theme = window.localStorage.getItem('robertWorkbenchTheme') || 'system'; state.language = window.localStorage.getItem('robertLanguage') || 'en'; setTheme(state.theme); applyTranslations(); if (window.matchMedia) { window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (state.theme === 'system') setTheme('system'); }); } }
    function workItems(data = state.data) { return data?.work_items || []; }
    function workSearchTokens() { return state.workSearchQuery.trim().toLowerCase().split(/\s+/).filter(Boolean); }
    function textHasTokens(value, tokens) { const text = String(value || '').toLowerCase(); return tokens.every((token) => text.includes(token)); }
    function workItemTitleMatches(item, tokens = workSearchTokens()) { return !tokens.length || textHasTokens(item?.source_title, tokens); }
    function taskIdMatchesWorkSearch(task, tokens = workSearchTokens()) { return !tokens.length || textHasTokens(task?.task_id, tokens); }
    function workItemMatchesSearch(item) { const tokens = workSearchTokens(); return !tokens.length || workItemTitleMatches(item, tokens) || (item.tasks || []).some((task) => taskIdMatchesWorkSearch(task, tokens)); }
    function workItemMatchesRepo(item) { if (!state.workRepoFilter) return true; return item?.repo_full_name === state.workRepoFilter || item?.repo_id === state.workRepoFilter; }
    function filteredWorkItems(data = state.data) { return workItems(data).filter((item) => workItemMatchesRepo(item) && workItemMatchesSearch(item)); }
    function filteredTasksForWorkItem(item) { const tasks = item?.tasks || []; const tokens = workSearchTokens(); if (!tokens.length || workItemTitleMatches(item, tokens)) return tasks; return tasks.filter((task) => taskIdMatchesWorkSearch(task, tokens)); }
    function selectedWorkItem() { const items = filteredWorkItems(); const selected = items.find((item) => item.id === state.selectedWorkItemId); if (selected) return selected; if (state.selectedTaskId) return items.find((item) => (item.tasks || []).some((task) => task.task_id === state.selectedTaskId)) || null; return items[0] || null; }
    function tasksForSelectedWorkItem() { return filteredTasksForWorkItem(selectedWorkItem()); }
    function selectedTask() { const tasks = tasksForSelectedWorkItem(); return tasks.find((task) => task.task_id === state.selectedTaskId) || tasks[0] || null; }
    function chooseDefaultTask(data) { const items = filteredWorkItems(data); const tasks = items.flatMap((item) => filteredTasksForWorkItem(item)); if (state.selectedTaskId && tasks.some((task) => task.task_id === state.selectedTaskId)) { const item = items.find((workItem) => (workItem.tasks || []).some((task) => task.task_id === state.selectedTaskId)); state.selectedWorkItemId = item?.id || state.selectedWorkItemId; return; } const attention = tasks.find((task) => task.operator_state === 'needs_attention'); const fallbackTask = attention || tasks[0] || null; state.selectedTaskId = fallbackTask?.task_id || null; const item = items.find((workItem) => (workItem.tasks || []).some((task) => task.task_id === state.selectedTaskId)); state.selectedWorkItemId = item?.id || items[0]?.id || null; }
    function hashFor(view, taskId) { return view === 'work' && taskId ? `#work/${encodeURIComponent(taskId)}` : `#${encodeURIComponent(view)}`; }
    function setView(view, updateHash = true) { const validViews = new Set(['command', 'work', 'daemon', 'timeline', 'knowledge', 'notifications']); state.currentView = validViews.has(view) ? view : 'command'; document.querySelectorAll('[data-view-panel]').forEach((panel) => { panel.hidden = panel.getAttribute('data-view-panel') !== state.currentView; }); document.querySelectorAll('[data-view]').forEach((button) => { button.classList.toggle('active', button.getAttribute('data-view') === state.currentView); }); document.querySelectorAll('[data-primary-view]').forEach((link) => { link.classList.toggle('selected', link.getAttribute('data-primary-view') === (state.currentView === 'knowledge' ? 'knowledge' : 'operations')); }); if (updateHash) { const nextHash = hashFor(state.currentView, state.selectedTaskId); if (window.location.hash !== nextHash) window.history.replaceState(null, '', nextHash); } }
    function applyHash() { const hash = window.location.hash.replace(/^#/, ''); if (!hash) { setView('command', false); return; } const [view, rawTaskId] = hash.split('/'); if (view === 'work' && rawTaskId) state.selectedTaskId = decodeURIComponent(rawTaskId); setView(view || 'command', false); }
    function openTask(taskId) { state.selectedTaskId = taskId; const item = workItems().find((workItem) => (workItem.tasks || []).some((task) => task.task_id === taskId)); if (item) { state.selectedWorkItemId = item.id; if (state.workRepoFilter && !workItemMatchesRepo(item)) state.workRepoFilter = ''; if (state.workSearchQuery && !workItemMatchesSearch(item)) state.workSearchQuery = ''; } closeArtifactDrawer(); setView('work'); renderAll(); }
    function openWorkItem(itemId) { state.selectedWorkItemId = itemId; const item = selectedWorkItem(); state.selectedTaskId = (item?.tasks || [])[0]?.task_id || null; closeArtifactDrawer(); setView('work'); renderAll(); }
    function toneForStatus(status) { const value = String(status || '').toLowerCase(); if (['failed', 'failure', 'error', 'rejected', 'timeout', 'expired'].some((item) => value.includes(item))) return 'critical'; if (['pending', 'stale', 'skipped', 'retry', 'permission'].some((item) => value.includes(item))) return 'warning'; if (['completed', 'succeeded', 'healthy', 'active', 'running', 'published', 'resolved'].some((item) => value.includes(item))) return 'ok'; return 'info'; }
    function runDisplayStatus(run) { return run?.summary?.overall_status || run?.status; }
    function renderMetrics(data) { const summary = data.summary || {}; const daemon = data.daemon || {}; const latestRun = daemon.latest_run || {}; const items = [[t('daemon'), latestRun.status ? dl(latestRun.status) : (daemon.available ? t('idleHealthy') : '-'), toneForStatus(latestRun.status || 'healthy')], [t('queue'), (data.wakeup_summary || {}).pending || 0, ((data.wakeup_summary || {}).pending || 0) ? 'info' : 'ok'], [t('attention'), (data.notification_center || []).filter((item) => item.severity === 'critical' || item.severity === 'warning').length, (data.notification_center || []).some((item) => item.severity === 'critical') ? 'critical' : 'warning'], [t('publish'), summary.pending_publish_actions || 0, (summary.pending_publish_actions || 0) ? 'info' : 'ok'], [t('latestRun'), summary.agent_runs || 0, '']]; $('summary-metrics').innerHTML = items.map(([label, value, tone]) => `<div class="health-card ${tone || ''}"><span>${esc(label)}</span><strong>${esc(value)}</strong>${tone ? badge(tone, tone) : ''}</div>`).join(''); const hasCritical = (data.notification_center || []).some((item) => item.severity === 'critical'); $('system-snapshot-status').textContent = hasCritical ? t('needsReview') : t('idleHealthy'); $('system-snapshot-status').className = `badge ${hasCritical ? 'critical' : 'ok'}`; $('system-snapshot').innerHTML = [['repos', summary.repos || 0], ['tasks', summary.tasks || 0], ['events', summary.github_events || 0], ['notifications', summary.notifications || 0], ['wakeups', (data.wakeup_summary || {}).pending || 0]].map(([label, value]) => `<div class="row-between"><span class="muted">${esc(dl(label))}</span><strong>${esc(value)}</strong></div>`).join(''); }
    function stateIcon(tone) { const value = tone || 'info'; const mark = value === 'critical' ? '!' : value === 'warning' ? '!' : value === 'ok' ? '✓' : 'i'; return `<span class="state-icon ${esc(value)}" aria-hidden="true">${esc(mark)}</span>`; }
    function renderInboxItem(item) { const taskButton = item.task_id ? `<button class="inline-button" type="button" data-open-task="${esc(item.task_id)}">${esc(item.task_id)}</button>` : ''; return `<article class="row-card ${esc(item.severity || 'info')}"><div class="issue-row">${stateIcon(item.severity || 'info')}<div><div class="item-title">${esc(dl(item.title || item.kind || 'event'))}</div><div class="muted">${esc(dl(item.summary || item.reason || item.status || ''))}</div><div class="issue-meta"><span>${fmt(item.created_at)}</span><span>${esc(item.source_key || dl(item.kind || ''))}</span>${taskButton}${item.github_url ? externalLink(item.github_url, t('openGithub')) : ''}</div></div><div class="issue-labels">${badge(item.severity || item.kind || 'info', item.severity || 'info')}${badge(item.kind || 'event', '')}</div></div>${metadataDetails(item.metadata)}</article>`; }
    function renderNextSteps(data) { const steps = (data.operator_next_steps || []).filter(matchesSearchObject); const notifications = (data.notification_center || []).filter((item) => item.severity === 'critical' || item.severity === 'warning').filter(matchesSearchObject); const items = steps.length ? steps.map((step) => ({ id: step.target_id || step.event_fingerprint, kind: step.target_kind, severity: step.severity, title: step.next_action || 'inspect', summary: step.reason || step.target_kind || '', created_at: step.created_at, task_id: step.target_kind === 'task' ? step.target_id : step.task_id, source_key: step.source_key || step.target_id, github_url: step.github_url || step.target_url, metadata: step })) : notifications; $('next-step-count').textContent = `${items.length} ${t('open')}`; $('operator-next-steps').innerHTML = items.length ? items.slice(0, 7).map(renderInboxItem).join('') : `<div class="empty">${esc(t('noOperatorAction'))}</div>`; bindTaskButtons(); }
    function renderNowLane(data) { const items = (data.work_items || []).filter(matchesSearchObject); $('overview-task-count').textContent = `${items.length} ${t('recent')}`; $('now-lane').innerHTML = items.slice(0, 8).map((item) => { const tone = severityByState[item.top_operator_state] || 'info'; return `<article class="task-card"><div class="issue-row">${stateIcon(tone)}<div><div class="item-title">${esc(sourceTitle(item))}</div><div class="issue-meta"><span>${esc(sourceMeta(item))}</span><span>${esc(item.latest_task_updated_at ? `${t('updated')} ${fmt(item.latest_task_updated_at)}` : '')}</span><button class="inline-button" type="button" data-work-item-id="${esc(item.id)}">${esc(t('inspect'))}</button>${item.github_url ? externalLink(item.github_url, t('openGithub')) : ''}</div></div><div class="issue-labels">${badge(stateLabel(item.top_operator_state), tone)}${badge(`${item.task_count || 0} ${t('tasks')}`, '')}</div></div></article>`; }).join('') || `<div class="empty">${esc(t('noWorkItems'))}</div>`; bindWorkItemButtons(); }
    function renderWorkFilters(data) { const repos = data.repos || []; const select = $('work-repo-filter'); const search = $('work-search'); if (search && search.value !== state.workSearchQuery) search.value = state.workSearchQuery; if (!select) return; if (state.workRepoFilter && !repos.some((repo) => (repo.full_name || repo.repo_id) === state.workRepoFilter)) state.workRepoFilter = ''; select.innerHTML = `<option value="">${esc(t('allProjects'))}</option>${repos.map((repo) => { const value = repo.full_name || repo.repo_id; return `<option value="${esc(value)}">${esc(value)}</option>`; }).join('')}`; select.value = state.workRepoFilter; }
    function renderTasks(data) { const items = filteredWorkItems(data); $('task-count').textContent = `${items.length} ${t('recent')}`; $('task-list').innerHTML = items.length ? items.map((item) => { const tone = severityByState[item.top_operator_state] || ''; return `<article class="task-card ${item.id === state.selectedWorkItemId ? 'active' : ''}"><button class="task-select" type="button" data-work-item-id="${esc(item.id)}"><div class="row-top"><div class="item-title">${esc(sourceTitle(item))}</div>${badge(stateLabel(item.top_operator_state), tone)}</div><div class="muted">${esc(sourceMeta(item) || item.workstream_id || '')}</div><div>${badge(`${item.task_count || 0} ${t('tasks')}`, '')}${item.latest_task_updated_at ? badge(fmtShort(item.latest_task_updated_at), '') : ''}</div></button>${item.github_url ? externalLink(item.github_url, t('openGithub')) : ''}</article>`; }).join('') : `<div class="empty">${esc(t('noWorkItems'))}</div>`; bindWorkItemButtons(); }
    function renderWorkTaskList() { const tasks = tasksForSelectedWorkItem(); $('work-task-count').textContent = `${tasks.length} ${t('recent')}`; $('work-task-list').innerHTML = tasks.length ? tasks.map((task) => { const tone = severityByState[task.operator_state] || ''; return `<article class="task-card ${task.task_id === state.selectedTaskId ? 'active' : ''}"><button class="task-select" type="button" data-task-id="${esc(task.task_id)}"><div class="row-top"><div class="item-title">${esc(task.route_id || task.expected_output || task.task_id)}</div>${badge(stateLabel(task.operator_state), tone)}</div><div class="muted">${esc(task.task_id)}${task.updated_at ? ` / ${esc(t('updated'))} ${esc(fmtShort(task.updated_at))}` : ''}</div><div>${badge(task.priority || 'P?', '')}${badge(task.route_id || task.expected_output || 'unrouted', '')}${task.latest_attempt_status ? badge(`attempt ${task.latest_attempt_status}`, '') : ''}</div></button>${task.github_url ? externalLink(task.github_url, t('openGithub')) : ''}</article>`; }).join('') : `<div class="empty">${esc(t('noTasks'))}</div>`; document.querySelectorAll('[data-task-id]').forEach((button) => { button.addEventListener('click', () => { state.selectedTaskId = button.getAttribute('data-task-id'); closeArtifactDrawer(); renderAll(); }); }); }
    function field(label, value) { const display = value === 0 ? '0' : (value || '-'); return `<div class="field"><label>${esc(dl(label))}</label><div>${esc(dl(display))}</div></div>`; }
    function artifactPreviewOpen() { return $('artifact-preview-drawer')?.classList.contains('open'); }
    function setArtifactPreviewTitle(label) { const title = $('artifact-preview-title'); if (title) title.textContent = label || t('artifactPreview'); }
    function openArtifactDrawer(label) { const drawer = $('artifact-preview-drawer'); const scrim = $('preview-scrim'); if (!drawer || !scrim) return; setArtifactPreviewTitle(label ? `${t('artifactPreview')}: ${label}` : t('artifactPreview')); drawer.classList.add('open'); scrim.classList.add('open'); drawer.setAttribute('aria-hidden', 'false'); document.body.classList.add('preview-open'); $('artifact-preview-close')?.focus(); }
    function closeArtifactDrawer() { const drawer = $('artifact-preview-drawer'); const scrim = $('preview-scrim'); if (!drawer || !scrim) return; drawer.classList.remove('open'); scrim.classList.remove('open'); drawer.setAttribute('aria-hidden', 'true'); document.body.classList.remove('preview-open'); setArtifactPreviewTitle(t('artifactPreview')); }
    function resetArtifactPreview(message, mode = 'placeholder') { const target = $('artifact-preview'); if (!target) return; target.className = `artifact-preview ${mode}`; target.textContent = message; }
    function renderTaskDetail() { renderWorkTaskList(); const task = selectedTask(); if (!task) { $('detail-status').textContent = ''; $('task-detail').innerHTML = `<div class="empty">${esc(t('noTasks'))}</div>`; $('artifact-count').textContent = dl('0 files'); $('artifact-list').innerHTML = `<div class="empty">${esc(t('noArtifacts'))}</div>`; closeArtifactDrawer(); resetArtifactPreview(t('selectArtifact')); return; } const tone = severityByState[task.operator_state] || ''; $('detail-status').innerHTML = badge(stateLabel(task.operator_state), tone); $('task-detail').innerHTML = `<div class="detail-title"><h3>${esc(sourceTitle(task))}</h3><p>${esc(sourceMeta(task) || task.workstream_id || '')}</p></div><div class="detail-actions">${task.github_url ? externalLink(task.github_url, t('openGithub')) : ''}</div><div class="meta-grid">${field('Lifecycle', task.lifecycle)}${field('Workstream', task.workstream_lifecycle)}${field('Route', task.route_id || task.expected_output)}${field('Route confidence', task.route_confidence)}${field('Latest attempt', task.latest_attempt_status)}${field('Heartbeat', fmt(task.latest_attempt_heartbeat_at))}${field('Updated', fmt(task.updated_at))}${field('Trigger', task.trigger_event_fingerprint)}</div><div class="subhead">${esc(t('source'))}</div><div class="meta-grid">${field('Title', task.source_title)}${field('Source key', task.source_key)}${field('Type and number', `${task.source_type || '-'} #${task.source_number || '-'}`)}${field('Task ID', task.task_id)}<div class="field"><label>GitHub</label><div>${externalLink(task.github_url)}</div></div>${field('Next action', task.next_action)}</div><div class="subhead">${esc(t('allowedActions'))}</div><div class="meta-grid"><div class="field"><label>${esc(dl('Actions'))}</label><div>${(task.allowed_github_actions || []).map((item) => `<span class="json-chip">${esc(item)}</span>`).join('') || '-'}</div></div><div class="field"><label>${esc(t('recommendedSkills'))}</label><div>${(task.recommended_skills || []).map((item) => `<span class="json-chip">${esc(item)}</span>`).join('') || '-'}</div></div></div>${task.latest_notification ? `<div class="subhead">${esc(t('latestNotification'))}</div><div class="meta-grid">${field('Type', task.latest_notification.notification_type)}${field('Status', task.latest_notification.status)}${field('Created', fmt(task.latest_notification.created_at))}${field('Channel', task.latest_notification.channel)}</div>${metadataDetails(task.latest_notification.metadata)}` : ''}`; renderArtifacts(task); }
    function renderCommandInspector(data) { const task = selectedTask(); const daemon = data.daemon || {}; const latestRun = daemon.latest_run || {}; const latestLease = daemon.latest_lease || {}; const firstNotice = (data.notification_center || [])[0] || null; if (!task) { const noticeTone = firstNotice ? (firstNotice.severity || 'info') : toneForStatus(latestRun.status || 'healthy'); $('command-inspector-status').innerHTML = badge(firstNotice ? (firstNotice.severity || firstNotice.kind) : (latestRun.status || t('idleHealthy')), noticeTone); $('command-inspector').innerHTML = `${firstNotice ? `<div class="detail-title"><h3>${esc(sourceTitle(firstNotice))}</h3><p>${esc(dl(firstNotice.summary || firstNotice.kind || ''))}</p></div><div class="detail-actions">${firstNotice.task_id ? `<button class="inline-button" type="button" data-open-task="${esc(firstNotice.task_id)}">${esc(firstNotice.task_id)}</button>` : ''}${firstNotice.github_url ? externalLink(firstNotice.github_url, t('openGithub')) : ''}</div><div class="meta-grid">${field('Kind', firstNotice.kind)}${field('Source', sourceMeta(firstNotice))}${field('Created', fmt(firstNotice.created_at))}${field('Severity', firstNotice.severity)}</div>` : `<div class="empty">${esc(t('noTasks'))}</div>`}<div class="subhead">${esc(t('daemon'))}</div><div class="meta-grid">${field('Run status', latestRun.status || (daemon.available ? t('idleHealthy') : '-'))}${field('Run ID', latestRun.daemon_run_id)}${field('Lease owner', latestLease.owner_id)}${field('Heartbeat', fmt(latestLease.heartbeat_at))}</div>`; bindTaskButtons(); return; } const tone = severityByState[task.operator_state] || ''; $('command-inspector-status').innerHTML = badge(stateLabel(task.operator_state), tone); $('command-inspector').innerHTML = `<div class="detail-title"><h3>${esc(sourceTitle(task))}</h3><p>${esc(sourceMeta(task) || task.workstream_id || '')}</p></div><div class="detail-actions">${task.github_url ? externalLink(task.github_url, t('openGithub')) : ''}<button class="inline-button" type="button" data-open-task="${esc(task.task_id)}">${esc(t('inspect'))}</button></div><div class="meta-grid">${field('Lifecycle', task.lifecycle)}${field('Route', task.route_id || task.expected_output)}${field('Task ID', task.task_id)}${field('Attempt', task.latest_attempt_status)}${field('Updated', fmt(task.updated_at))}</div><div class="subhead">${esc(t('daemon'))}</div><div class="meta-grid">${field('Run status', latestRun.status)}${field('Lease owner', latestLease.owner_id)}${field('Heartbeat', fmt(latestLease.heartbeat_at))}${field('Expires', fmt(latestLease.expires_at))}</div>${task.latest_notification ? `<div class="subhead">${esc(t('latestNotification'))}</div><div class="meta-grid">${field('Type', task.latest_notification.notification_type)}${field('Status', task.latest_notification.status)}</div>` : ''}`; bindTaskButtons(); }
    function renderArtifacts(task) { const artifacts = Object.entries(task.artifacts || {}); $('artifact-count').textContent = `${artifacts.length} ${dl('files')}`; $('artifact-list').innerHTML = artifacts.length ? artifacts.map(([type, artifact]) => `<div class="artifact-row"><div class="row-between"><strong>${esc(type)}</strong>${badge(artifact.bytes ? `${artifact.bytes} ${dl('bytes')}` : dl('size unknown'), '')}</div><div class="muted">${esc(artifact.path || '')}</div><div class="artifact-actions"><button class="inline-button" type="button" data-preview-task="${esc(task.task_id)}" data-preview-artifact="${esc(type)}">${esc(t('preview'))}</button></div></div>`).join('') : `<div class="empty">${esc(t('noArtifacts'))}</div>`; if (!artifactPreviewOpen()) resetArtifactPreview(t('selectArtifact')); document.querySelectorAll('[data-preview-task]').forEach((button) => { button.addEventListener('click', () => previewArtifact(button.getAttribute('data-preview-task'), button.getAttribute('data-preview-artifact'))); }); }
    function parseArtifactPreview(text) { const parts = String(text || '').split(/\n\n/); const headerText = parts.length > 1 ? parts.shift() : ''; const fields = {}; headerText.split(/\n/).forEach((line) => { const match = line.match(/^([^=]+)=(.*)$/); if (match) fields[match[1]] = match[2]; }); return { fields, content: parts.join('\n\n') }; }
    function artifactPreviewHeader(fields) { const rows = ['task_id', 'artifact_type', 'path', 'truncated'].filter((key) => fields[key]).map((key) => `<span>${esc(key)}=${esc(fields[key])}</span>`).join(''); return rows ? `<div class="artifact-preview-header">${rows}</div>` : ''; }
    function safeMarkdownHref(url) { const value = String(url || '').trim(); return /^(https?:\/\/|mailto:|#)/i.test(value) ? value : ''; }
    function inlineMarkdown(text) { let html = esc(text); html = html.replace(/`([^`]+)`/g, '<code>$1</code>'); html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>'); html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_match, label, url) => { const href = safeMarkdownHref(url); return href ? `<a class="external-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label; }); return html; }
    function renderCodeBlock(code, language = '') { const text = String(code || ''); const lang = String(language || '').trim().toLowerCase(); if (lang === 'json') return `<pre class="gh-code"><code>${highlightJson(text) || esc(text)}</code></pre>`; return `<pre class="gh-code"><code>${esc(text)}</code></pre>`; }
    function renderMarkdown(content) { const lines = String(content || '').replace(/\r\n/g, '\n').split('\n'); const html = []; let paragraph = []; let list = []; const flushParagraph = () => { if (paragraph.length) { html.push(`<p>${inlineMarkdown(paragraph.join(' '))}</p>`); paragraph = []; } }; const flushList = () => { if (list.length) { html.push(`<ul>${list.map((item) => `<li>${inlineMarkdown(item)}</li>`).join('')}</ul>`); list = []; } }; for (let index = 0; index < lines.length; index += 1) { const line = lines[index]; const fence = line.match(/^```([A-Za-z0-9_-]*)\s*$/); if (fence) { flushParagraph(); flushList(); const code = []; index += 1; while (index < lines.length && !lines[index].match(/^```\s*$/)) { code.push(lines[index]); index += 1; } html.push(renderCodeBlock(code.join('\n'), fence[1])); continue; } const heading = line.match(/^(#{1,3})\s+(.+)$/); if (heading) { flushParagraph(); flushList(); const level = heading[1].length; html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`); continue; } const item = line.match(/^\s*[-*]\s+(.+)$/); if (item) { flushParagraph(); list.push(item[1]); continue; } if (line.startsWith('> ')) { flushParagraph(); flushList(); html.push(`<blockquote>${inlineMarkdown(line.slice(2))}</blockquote>`); continue; } if (!line.trim()) { flushParagraph(); flushList(); continue; } paragraph.push(line.trim()); } flushParagraph(); flushList(); return html.join('') || renderPlainText(content); }
    function highlightJson(content) { try { const pretty = JSON.stringify(JSON.parse(content), null, 2); return pretty.replace(/("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g, (token) => { let cls = 'json-number'; if (token.endsWith(':')) cls = 'json-key'; else if (token.startsWith('"')) cls = 'json-string'; else if (/true|false|null/.test(token)) cls = 'json-literal'; return `<span class="${cls}">${esc(token)}</span>`; }); } catch (_error) { return ''; } }
    function renderPlainText(content) { return `<pre class="gh-code"><code>${esc(content || '')}</code></pre>`; }
    function artifactPreviewMode(fields, artifactType, content) { const path = String(fields.path || '').toLowerCase(); const type = String(artifactType || fields.artifact_type || '').toLowerCase(); const trimmed = String(content || '').trim(); if (path.endsWith('.md') || path.endsWith('.markdown') || type.includes('markdown') || type.includes('prompt') || type.includes('handoff')) return 'markdown'; if (path.endsWith('.json') || type.includes('json')) return 'json'; if ((trimmed.startsWith('{') || trimmed.startsWith('[')) && highlightJson(trimmed)) return 'json'; return 'text'; }
    function renderArtifactPreview(text, artifactType) { const target = $('artifact-preview'); if (!target) return; const parsed = parseArtifactPreview(text); const mode = artifactPreviewMode(parsed.fields, artifactType, parsed.content); const highlightedJson = mode === 'json' ? highlightJson(parsed.content) : ''; const body = mode === 'markdown' ? renderMarkdown(parsed.content) : highlightedJson ? `<pre class="gh-code"><code>${highlightedJson}</code></pre>` : renderPlainText(parsed.content); target.className = `artifact-preview ${mode}`; target.innerHTML = artifactPreviewHeader(parsed.fields) + body; }
    async function previewArtifact(taskId, artifactType) { openArtifactDrawer(artifactType); resetArtifactPreview(t('artifactLoading')); const url = `/artifact.txt?task_id=${encodeURIComponent(taskId)}&artifact_type=${encodeURIComponent(artifactType)}`; try { const response = await fetch(url, { cache: 'no-store' }); const text = await response.text(); if (response.ok) renderArtifactPreview(text, artifactType); else resetArtifactPreview(`${t('artifactUnavailable')}: ${text}`, 'error'); } catch (error) { resetArtifactPreview(`${t('artifactUnavailable')}: ${error.message}`, 'error'); } }
    function daemonLeaseStatus(lease) { if (!lease) return null; if (lease.status === 'active') { const expiresAt = new Date(lease.expires_at); if (!Number.isNaN(expiresAt.getTime()) && expiresAt.getTime() <= Date.now()) return 'expired'; } return lease.status || null; }
    function renderDaemon(data) { const daemon = data.daemon || {}; const latestRun = daemon.latest_run || null; const latestEvent = daemon.latest_event || null; const latestLease = daemon.latest_lease || null; const leaseStatus = daemonLeaseStatus(latestLease); const displayStatus = leaseStatus === 'expired' ? 'expired' : latestRun?.status; const recentEvents = daemon.recent_events || (latestEvent ? [latestEvent] : []); $('daemon-run-status').innerHTML = latestRun ? badge(displayStatus || 'unknown', toneForStatus(displayStatus)) : ''; $('daemon-health').innerHTML = (latestRun || latestEvent || latestLease) ? `${field('Run ID', latestRun?.daemon_run_id)}${field('Run status', latestRun?.status)}${field('Started', fmt(latestRun?.started_at))}${field('Finished', fmt(latestRun?.finished_at))}${field('Config', latestRun?.config_path)}${field('Owner', latestRun?.owner_id)}${field('Lease status', leaseStatus)}${field('Lease owner', latestLease?.owner_id)}${field('Lease key', latestLease?.resource_key)}${field('Heartbeat', fmt(latestLease?.heartbeat_at))}${field('Expires', fmt(latestLease?.expires_at))}${field('Latest event', latestEvent?.event_type)}` : `<div class="empty">${esc(t('noDaemon'))}</div>`; $('daemon-event-count').textContent = `${recentEvents.length} ${t('recent')}`; $('daemon-events').innerHTML = recentEvents.length ? recentEvents.map((event) => `<article class="row-card"><div class="row-between"><strong>${esc(dl(event.event_type || 'daemon_event'))}</strong>${badge(event.status || 'unknown', toneForStatus(event.status))}</div><div class="muted">${fmt(event.created_at)} / ${esc(event.daemon_run_id || '-')}</div>${metadataDetails(event.metadata)}</article>`).join('') : `<div class="empty">${esc(t('noDaemonEvents'))}</div>`; }
    function timelineItems(data) { const items = []; (data.notification_center || []).forEach((item) => { items.push({ id: item.id, kind: item.kind, type: item.kind === 'daemon_event' ? (item.title || item.kind) : item.kind, title: sourceTitle(item), summary: item.summary || item.title || '', at: item.created_at, severity: item.severity, task_id: item.task_id, github_url: item.github_url, source_key: item.source_key, source_title: item.source_title, metadata: item.metadata }); }); (data.recent_events || []).forEach((event) => { items.push({ id: event.event_id, kind: 'github_event', type: event.event_type || 'github_event', title: sourceTitle(event), summary: `${event.event_type || 'event'} / ${event.authorization_status || ''}`, at: event.event_at, severity: toneForStatus(event.authorization_status), task_id: event.task_id, github_url: event.html_url, source_key: event.source_key, source_title: event.source_title, metadata: event }); }); (data.recent_worker_results || []).forEach((result) => { items.push({ id: result.result_id, kind: 'worker_result', type: result.output_type || 'worker_result', title: result.output_type || 'worker_result', summary: result.handoff || result.task_id || '', at: result.created_at, severity: 'ok', task_id: result.task_id, metadata: result }); }); (data.recent_actions || []).forEach((action) => { items.push({ id: action.action_id, kind: 'github_action', type: action.action_type || 'github_action', title: action.action_type || 'github_action', summary: `${action.audit_status || '-'} / ${action.publish_status || '-'}`, at: action.created_at, severity: toneForStatus(`${action.audit_status || ''} ${action.publish_status || ''}`), task_id: action.task_id, github_url: action.target_url, metadata: action }); }); (data.recent_runs || []).forEach((run) => { const status = runDisplayStatus(run); items.push({ id: run.run_id, kind: 'run', type: 'run', title: run.run_id, summary: status || '', at: run.started_at, severity: toneForStatus(status), metadata: run }); }); return items.sort((a, b) => String(b.at || '').localeCompare(String(a.at || ''))); }
    function filteredTimelineItems(data) { return timelineItems(data).filter((item) => !state.timelineHiddenTypes.has(item.type || item.kind)); }
    function renderTimelineFilters(data) { const types = Array.from(new Set(timelineItems(data).map((item) => item.type || item.kind).filter(Boolean))).sort(); $('timeline-filters').innerHTML = types.length ? types.map((type) => `<label class="checkbox-pill"><input type="checkbox" data-timeline-type="${esc(type)}" ${state.timelineHiddenTypes.has(type) ? '' : 'checked'}><span>${esc(dl(type))}</span></label>`).join('') : ''; document.querySelectorAll('[data-timeline-type]').forEach((input) => { input.addEventListener('change', () => { const type = input.getAttribute('data-timeline-type'); if (input.checked) state.timelineHiddenTypes.delete(type); else state.timelineHiddenTypes.add(type); renderTimeline(state.data); renderNavCounts(state.data); }); }); }
    function timelineCard(item) { return `<article class="timeline-row ${esc(item.severity || 'info')}"><div class="row-between"><div><div class="item-title">${esc(dl(item.title || item.id))}${dl(item.title) !== item.title ? `<code class="muted"> (${esc(item.title)})</code>` : ''}</div><div class="muted">${esc(dl(item.summary || item.kind || ''))}</div></div>${badge(item.kind || 'event', item.severity || 'info')}</div><div class="timeline-meta"><span>${fmt(item.at)}</span>${sourceMeta(item) ? `<span>${esc(sourceMeta(item))}</span>` : ''}${item.task_id ? `<button class="inline-button" type="button" data-open-task="${esc(item.task_id)}">${esc(item.task_id)}</button>` : ''}${item.github_url ? externalLink(item.github_url, t('openGithub')) : ''}</div>${metadataDetails(item.metadata)}</article>`; }
    function renderTimeline(data) { renderTimelineFilters(data); const items = filteredTimelineItems(data).slice(0, 80); $('timeline-count').textContent = `${items.length} ${t('recent')}`; $('timeline-list').innerHTML = items.length ? items.map(timelineCard).join('') : `<div class="empty">${esc(t('noTimeline'))}</div>`; bindTaskButtons(); }
    function renderNotifications(data) { document.querySelectorAll('[data-dynamic-label]').forEach((el) => { el.textContent = dl(el.dataset.dynamicLabel); }); const all = data.notification_center || []; const severity = state.notificationSeverity; const items = all.filter((item) => !severity || (severity === 'attention' ? ['critical', 'warning'].includes(item.severity) : (item.severity || 'info') === severity)); $('notification-count').textContent = `${items.length} / ${all.length} ${t('recent')}`; $('notification-center').innerHTML = items.length ? items.map((item) => `<article class="notification-card ${esc(item.severity || 'info')}"><div class="row-between"><div><div class="item-title">${esc(dl(sourceTitle(item)))}</div><div class="muted">${esc(dl(item.summary || item.title || ''))}</div></div>${badge(item.severity || 'info', item.severity || 'info')}</div><div class="timeline-meta"><span>${fmt(item.created_at)}</span><span>${esc(dl(item.kind || ''))}</span>${sourceMeta(item) ? `<span>${esc(sourceMeta(item))}</span>` : ''}${item.github_url ? externalLink(item.github_url, t('openGithub')) : ''}</div><div class="action-row">${item.task_id ? `<button class="inline-button" type="button" data-open-task="${esc(item.task_id)}">${esc(t('inspect'))}</button>` : ''}</div>${metadataDetails(item.metadata)}</article>`).join('') : `<div class="empty">${esc(t(severity ? 'noMatchingNotifications' : 'noNotifications'))}</div>`; bindTaskButtons(); }
    function candidateCard(candidate) { const evidence = (candidate.evidence || []).map((item) => `<span class="json-chip">${esc(item.type || 'evidence')}: ${esc(item.value || '')}</span>`).join(''); return `<article class="row-card"><div class="row-between"><div><strong>${esc(candidate.title)}</strong><div class="muted">${esc(candidate.summary)}</div></div>${badge(candidate.candidate_type || 'rule', '')}</div><pre>${esc(candidate.prompt_text || '')}</pre><div>${evidence || `<span class="muted">${esc(t('noActivity'))}</span>`}</div><form class="form-grid" onsubmit="return approveKnowledge(event, '${esc(candidate.candidate_id)}')"><label>${esc(t('scopeType'))}<select name="scope_type"><option value="route">route</option><option value="path">path</option><option value="symbol">symbol</option><option value="workstream">workstream</option><option value="global">global</option></select></label><label>${esc(t('scopeValue'))}<input name="scope_value" value=""></label><label>${esc(t('approvedBy'))}<input name="approved_by" value="${esc(window.localStorage.getItem('ddReviewer') || '')}" required></label><div class="action-row"><button class="primary-button" type="submit">${esc(t('approve'))}</button><button class="danger-button" type="button" onclick="rejectKnowledge('${esc(candidate.candidate_id)}')">${esc(t('reject'))}</button></div></form></article>`; }
    function renderKnowledge(data) { const review = data.knowledge_review || {}; const counts = review.counts || {}; const pending = review.pending_candidates || []; const active = review.active_knowledge || []; renderKnowledgeProposal(review); $('knowledge-count').textContent = `${counts.pending || 0} ${t('pending')}`; $('runtime-knowledge-count').textContent = `${counts.active || 0} ${t('active')}`; $('knowledge-candidates').innerHTML = review.available === false ? `<div class="empty">${esc(t('noKnowledgeTables'))}</div>` : pending.map(candidateCard).join('') || `<div class="empty">${esc(t('noPendingKnowledge'))}</div>`; $('runtime-knowledge-list').innerHTML = active.map((item) => `<article class="row-card"><div class="row-between"><strong>${esc(item.title)}</strong>${badge(`${item.scope_type}:${item.scope_value || 'global'}`, '')}</div><div class="muted">${esc(t('approvedBy'))} ${esc(item.approved_by)} / ${fmt(item.approved_at)}</div><pre>${esc(item.prompt_text || '')}</pre></article>`).join('') || `<div class="empty">${esc(t('noActiveKnowledge'))}</div>`; }
    function renderKnowledgeProposal(review) { const form = $('knowledge-propose-form'); const select = $('knowledge-propose-repo'); const repos = review.proposal_repos || []; select.innerHTML = repos.map((repo) => `<option value="${esc(repo.repo_id)}">${esc(repo.full_name || repo.repo_id)} (${repo.memory_count || 0} ${dl('memories')})</option>`).join(''); const disabled = review.available === false || repos.length === 0; form.querySelector('button').disabled = disabled; select.disabled = disabled; if (disabled && !repos.length) select.innerHTML = `<option value="">${esc(dl('No repositories'))}</option>`; }
    let controlSessionPromise = null;
    async function controlSession() { if (!controlSessionPromise) controlSessionPromise = fetch('/api/session', { cache: 'no-store' }).then((response) => response.json()); return controlSessionPromise; }
    async function postForm(url, formData) { const session = await controlSession(); const response = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/x-www-form-urlencoded', 'x-robert-csrf-token': session.csrf_token || '' }, body: new URLSearchParams(formData) }); const result = await response.json(); if (!result.ok) throw new Error(result.safe_error || result.status || 'request failed'); await loadDashboard(); setView('knowledge'); return result; }
    function approveKnowledge(event, candidateId) { event.preventDefault(); const form = new FormData(event.currentTarget); const approvedBy = form.get('approved_by') || ''; window.localStorage.setItem('ddReviewer', approvedBy); form.set('candidate_id', candidateId); postForm('/knowledge/approve', form).catch((error) => alert(error.message)); return false; }
    function rejectKnowledge(candidateId) { const reviewer = window.localStorage.getItem('ddReviewer') || window.prompt(t('reviewer')) || ''; if (!reviewer) return; const reviewNote = window.prompt(t('rejectReason')) || ''; postForm('/knowledge/reject', { candidate_id: candidateId, reviewer: reviewer, review_note: reviewNote }).catch((error) => alert(error.message)); }
    function proposeKnowledge(event) { event.preventDefault(); const form = new FormData(event.currentTarget); postForm('/knowledge/propose', form).catch((error) => alert(error.message)); return false; }
    function renderCommandActivity(data) { const items = (data.notification_center || []).filter(matchesSearchObject).slice(0, 8); $('activity-count').textContent = `${items.length} ${t('recent')}`; $('command-activity').innerHTML = items.length ? items.map(timelineCard).join('') : `<div class="empty">${esc(t('noActivity'))}</div>`; bindTaskButtons(); }
    function bindTaskButtons() { document.querySelectorAll('[data-open-task]').forEach((button) => { button.addEventListener('click', () => openTask(button.getAttribute('data-open-task'))); }); }
    function bindWorkItemButtons() { document.querySelectorAll('[data-work-item-id]').forEach((button) => { button.addEventListener('click', () => openWorkItem(button.getAttribute('data-work-item-id'))); }); }
    function renderAll() { const data = state.data; if (!data) return; $('repo-context').textContent = repoContext(data); $('dashboard-subtitle').textContent = `${data.summary?.repos || 0} ${dl('repo')} / ${data.summary?.workstreams || 0} ${dl('workstreams')} / ${data.summary?.github_events || 0} ${dl('events')}`; $('last-updated').textContent = `${t('updated')} ${fmt(new Date())}`; $('refresh-status').textContent = $('auto-refresh-toggle').checked ? `${t('every')} ${Number(state.autoRefreshIntervalMs) / 1000}s` : t('off'); renderNavCounts(data); renderMetrics(data); renderNextSteps(data); renderNowLane(data); renderCommandActivity(data); renderCommandInspector(data); renderDaemon(data); renderWorkFilters(data); renderTasks(data); renderTaskDetail(); renderTimeline(data); renderNotifications(data); renderKnowledge(data); }
    async function loadDashboard(options = {}) { try { if (options.reason === 'auto' && document.hidden) return; if (options.reason === 'auto') $('refresh-status').textContent = t('refresh'); const response = await fetch('/data.json', { cache: 'no-store' }); if (!response.ok) throw new Error(`HTTP ${response.status}`); state.data = await response.json(); chooseDefaultTask(state.data); renderAll(); } catch (error) { $('dashboard-app').insertAdjacentHTML('beforeend', `<div class="error">${esc(t('failedLoad'))}: ${esc(error.message)}</div>`); } finally { if ($('auto-refresh-toggle').checked) $('refresh-status').textContent = `${t('every')} ${Number(state.autoRefreshIntervalMs) / 1000}s`; } }
    function stopAutoRefresh() { if (state.autoRefreshTimer) window.clearInterval(state.autoRefreshTimer); state.autoRefreshTimer = null; }
    function startAutoRefresh() { stopAutoRefresh(); state.autoRefreshTimer = window.setInterval(() => loadDashboard({ reason: 'auto' }), state.autoRefreshIntervalMs); }
    function configureAutoRefresh() { const enabled = $('auto-refresh-toggle').checked; state.autoRefreshIntervalMs = Number($('refresh-interval').value || 30000); window.localStorage.setItem('ddAutoRefreshEnabled', enabled ? '1' : '0'); window.localStorage.setItem('ddAutoRefreshIntervalMs', String(state.autoRefreshIntervalMs)); $('refresh-status').textContent = enabled ? `${t('every')} ${state.autoRefreshIntervalMs / 1000}s` : t('off'); if (enabled) startAutoRefresh(); else stopAutoRefresh(); }
    function initAutoRefresh() { const savedInterval = Number(window.localStorage.getItem('ddAutoRefreshIntervalMs') || 30000); state.autoRefreshIntervalMs = [15000, 30000, 60000].includes(savedInterval) ? savedInterval : 30000; $('refresh-interval').value = String(state.autoRefreshIntervalMs); $('auto-refresh-toggle').checked = window.localStorage.getItem('ddAutoRefreshEnabled') === '1'; configureAutoRefresh(); $('auto-refresh-toggle').addEventListener('change', configureAutoRefresh); $('refresh-interval').addEventListener('change', configureAutoRefresh); document.addEventListener('visibilitychange', () => { if (!document.hidden && $('auto-refresh-toggle').checked) loadDashboard({ reason: 'visible' }); }); }
    $('notification-severity').addEventListener('change', (event) => { state.notificationSeverity = event.target.value; renderNotifications(state.data || {}); });
    $('theme-select').addEventListener('change', (event) => setTheme(event.target.value));
    $('language-select').addEventListener('change', (event) => setLanguage(event.target.value));
    $('work-search').addEventListener('input', (event) => { state.workSearchQuery = event.target.value || ''; chooseDefaultTask(state.data); renderAll(); });
    $('work-repo-filter').addEventListener('change', (event) => { state.workRepoFilter = event.target.value || ''; chooseDefaultTask(state.data); renderAll(); });
    document.querySelectorAll('[data-view]').forEach((button) => { button.addEventListener('click', () => setView(button.getAttribute('data-view'))); });
    document.querySelectorAll('[data-close-artifact-preview]').forEach((button) => { button.addEventListener('click', closeArtifactDrawer); });
    window.addEventListener('hashchange', applyHash);
    window.addEventListener('keydown', (event) => { if (event.key === 'Escape' && artifactPreviewOpen()) closeArtifactDrawer(); });
    $('knowledge-propose-form').addEventListener('submit', proposeKnowledge);
    $('refresh-button').addEventListener('click', () => loadDashboard({ reason: 'manual' }));
    applyPreferences();
    applyHash();
    initAutoRefresh();
    controlSession().then((session) => {
      if (!session.writes_enabled) {
        const notice = document.createElement('p');
        notice.className = 'snapshot-notice';
        notice.setAttribute('role', 'status');
        notice.textContent = session.write_error || 'Read-only mode';
        document.querySelector('.mission-workspace').prepend(notice);
      }
    }).catch(() => {});
    loadDashboard();
  </script>
</body>
</html>
"""
