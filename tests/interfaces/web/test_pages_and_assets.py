import json
from tests.support.summary_database import SummaryFixture


class WebPagesAndAssetsTests(SummaryFixture):
    def test_web_shell_renders_failed_task_retrigger_control(self):
        from robert_agent.interfaces.web import server as web

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        status, _headers, body = web.build_http_response("/assets/workbench.js", db_path)

        self.assertEqual(status, 200)
        self.assertIn(b"retriggerTask", body)
        self.assertIn(b"/api/tasks/", body)
        self.assertIn("重新触发".encode(), body)

    def test_web_renders_operator_dashboard_html_shell(self):
        from robert_agent.interfaces.web import server as web

        html = web.render_dashboard_html()

        self.assertIn('<main id="dashboard-app"', html)
        self.assertIn('rel="icon" type="image/svg+xml"', html)
        self.assertNotIn('href="data:,"', html)
        self.assertIn("mission-shell", html)
        self.assertIn("fetch('/data.json'", html)
        self.assertIn('data-view="command"', html)
        self.assertIn('id="view-work"', html)
        self.assertIn('data-view="daemon"', html)
        self.assertIn('id="view-daemon"', html)
        self.assertIn('data-view="timeline"', html)
        self.assertIn('id="view-knowledge"', html)
        self.assertIn('data-view="notifications"', html)
        self.assertLess(html.index('data-view="notifications"'), html.index('data-view="daemon"'))
        self.assertIn("theme-select", html)
        self.assertIn("language-select", html)
        self.assertIn("notification-center", html)
        self.assertIn("detail-drawer", html)
        self.assertIn("work-task-list", html)
        self.assertLess(html.index('id="work-task-list"'), html.index('id="detail-drawer"'))
        self.assertNotIn('id="command-search"', html)
        self.assertNotIn("searchPlaceholder", html)
        self.assertIn('id="work-search"', html)
        self.assertIn('id="work-repo-filter"', html)
        self.assertIn("workSearchQuery", html)
        self.assertIn("workRepoFilter", html)
        self.assertIn("function filteredWorkItems", html)
        self.assertIn("function workItemTitleMatches", html)
        self.assertIn("function taskIdMatchesWorkSearch", html)
        self.assertIn("renderWorkFilters(data)", html)
        self.assertIn(
            "grid-template-columns: minmax(260px, 340px) minmax(260px, 340px) minmax(0, 1fr)",
            html,
        )
        self.assertIn("timeline-filters", html)
        self.assertIn("timelineHiddenTypes: new Set(['daemon_heartbeat'])", html)
        self.assertIn("function fmtShort(value)", html)
        self.assertIn("badge(fmtShort(item.latest_task_updated_at)", html)
        self.assertIn("${esc(fmtShort(task.updated_at))}", html)
        self.assertIn("field('Updated', fmt(task.updated_at))", html)
        self.assertIn("${date.getFullYear()}-${pad2(date.getMonth() + 1)}", html)
        self.assertIn("brand-mark", html)

        self.assertIn("const translations", html)
        self.assertIn("setTheme(", html)
        self.assertIn("setLanguage(", html)
        self.assertIn("localStorage.getItem('robertWorkbenchTheme')", html)
        self.assertIn("localStorage.getItem('robertLanguage')", html)
        self.assertIn("function repoContext(data) { const repos = data.repos || []", html)
        self.assertNotIn("value.match(/github:", html)
        self.assertIn("operator-next-steps", html)
        self.assertIn("daemon-health", html)
        self.assertIn("daemon-events", html)
        self.assertIn("renderDaemon(data)", html)
        self.assertIn("renderNotifications(data)", html)
        self.assertIn("artifact-preview", html)
        self.assertIn("artifact-preview-drawer", html)
        self.assertIn("preview-scrim", html)
        self.assertIn("data-close-artifact-preview", html)
        self.assertIn('class="artifact-preview placeholder"', html)
        self.assertIn("openArtifactDrawer", html)
        self.assertIn("closeArtifactDrawer", html)
        self.assertIn("renderMarkdown(content)", html)
        self.assertIn("highlightJson(content)", html)
        self.assertIn("gh-code", html)
        self.assertIn("json-key", html)
        self.assertIn("/knowledge/propose", html)
        self.assertIn("auto-refresh-toggle", html)
        self.assertIn("refresh-interval", html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn("location.hash", html)
        self.assertIn("openTask(", html)
        self.assertIn("previewArtifact", html)
        self.assertIn("sourceTitle(item)", html)
        self.assertIn("sourceMeta(item)", html)

    def test_web_pages_share_primary_navigation_and_language_preference(self):
        from robert_agent.interfaces.web import server as web
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.root / "shared-navigation.sqlite3"
        storage.init_database(db_path)

        operations_html = web.render_dashboard_html()
        self.assertIn('href="/assets/github-shell.css"', operations_html)
        self.assertIn('href="/assets/operations.css"', operations_html)
        self.assertIn('class="global-bar"', operations_html)
        self.assertIn('class="brand"', operations_html)
        self.assertIn('class="underline-nav"', operations_html)
        self.assertIn('class="underline-nav-item" href="/work"', operations_html)
        self.assertIn('class="underline-nav-item" href="/board"', operations_html)
        self.assertIn(
            'class="underline-nav-item selected" data-primary-view="operations" href="/operations"',
            operations_html,
        )
        self.assertIn('data-i18n="primaryWork"', operations_html)
        self.assertIn('data-i18n="navHistory"', operations_html)
        self.assertIn('data-i18n="navKnowledge"', operations_html)
        self.assertIn("navBoard: 'Board'", operations_html)
        self.assertIn("navBoard: '任务看板'", operations_html)
        self.assertNotIn('class="nav-button" type="button" data-view="work"', operations_html)
        self.assertNotIn('class="nav-button" type="button" data-view="knowledge"', operations_html)
        self.assertIn('id="nav-count-command">0</span>', operations_html)
        self.assertIn('id="nav-count-timeline">0</span>', operations_html)
        self.assertIn('id="nav-count-notifications">0</span>', operations_html)
        self.assertIn('id="nav-count-daemon">0</span>', operations_html)
        self.assertNotIn('id="global-search"', operations_html)

        status, _headers, board_html = web.build_http_response("/board", db_path)
        self.assertEqual(status, 200)
        self.assertIn(b'class="global-bar"', board_html)
        self.assertIn(b'href="/assets/github-shell.css"', board_html)
        self.assertIn(b'class="brand"', board_html)
        self.assertIn(b'class="underline-nav"', board_html)
        self.assertIn(b'class="underline-nav-item" href="/work"', board_html)
        self.assertIn(b'class="underline-nav-item selected" href="/board"', board_html)
        self.assertIn(b'class="underline-nav-item" href="/operations"', board_html)
        self.assertIn(b'data-i18n="navWork"', board_html)
        self.assertIn(b'data-i18n="navHistory"', board_html)
        self.assertIn(b'data-i18n="navKnowledge"', board_html)
        self.assertIn(b'id="language-select"', board_html)
        self.assertIn(b'id="theme-select"', board_html)
        self.assertIn(b'data-i18n="newTask"', board_html)
        self.assertNotIn(b'id="global-search"', board_html)

        status, _headers, workbench_html = web.build_http_response("/work", db_path)
        self.assertEqual(status, 200)
        self.assertIn(b'id="language-select"', workbench_html)
        self.assertIn(b'id="theme-select"', workbench_html)
        self.assertIn(b'data-i18n="navWork"', workbench_html)
        self.assertIn(b'data-i18n="navHistory"', workbench_html)
        self.assertIn(b'data-i18n-placeholder="workSearchPlaceholder"', workbench_html)
        self.assertNotIn(b'id="global-search"', workbench_html)

        _status, _headers, board_css = web.build_http_response("/assets/board.css", db_path)
        self.assertNotIn(b"Avenir Next Condensed", board_css)

        shell_status, shell_headers, shell_css = web.build_http_response("/assets/github-shell.css", db_path)
        self.assertEqual(shell_status, 200)
        self.assertEqual(shell_headers["content-type"], "text/css; charset=utf-8")
        self.assertIn(b"--bgColor-default", shell_css)
        self.assertIn(b".global-bar", shell_css)
        self.assertIn(b".underline-nav", shell_css)
        self.assertIn(b"-apple-system", shell_css)

        operations_status, operations_headers, operations_css = web.build_http_response(
            "/assets/operations.css", db_path
        )
        self.assertEqual(operations_status, 200)
        self.assertEqual(operations_headers["content-type"], "text/css; charset=utf-8")
        self.assertIn(b".mission-shell > .global-bar", operations_css)
        self.assertIn(b"var(--bgColor-default)", operations_css)

        _status, _headers, board_js = web.build_http_response("/assets/board.js", db_path)
        self.assertIn(b'localStorage.getItem("robertLanguage")', board_js)
        self.assertIn(b'localStorage.setItem("robertLanguage"', board_js)
        self.assertIn(b'localStorage.getItem("robertWorkbenchTheme")', board_js)
        self.assertIn(b'localStorage.setItem("robertWorkbenchTheme"', board_js)
        self.assertIn(b"function applyTranslations()", board_js)

        _status, _headers, workbench_js = web.build_http_response("/assets/workbench.js", db_path)
        self.assertIn(b'localStorage.getItem("robertLanguage")', workbench_js)
        self.assertIn(b'localStorage.setItem("robertLanguage"', workbench_js)
        self.assertIn(b'localStorage.getItem("robertWorkbenchTheme")', workbench_js)
        self.assertIn(b'localStorage.setItem("robertWorkbenchTheme"', workbench_js)
        self.assertIn(b"function applyTranslations()", workbench_js)

    def test_web_routes_root_to_html_and_data_json_to_payload(self):
        from robert_agent.interfaces.web import server as web

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        status, headers, body = web.build_http_response("/", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn(b'<main id="board-app"', body)

        status, headers, body = web.build_http_response("/operations", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn(b'<main id="dashboard-app"', body)

        status, headers, body = web.build_http_response("/data.json", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["summary"]["tasks"], 2)

        status, headers, body = web.build_http_response("/history", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["recent_tasks"][0]["task_id"], "task-2")

    def test_web_serves_board_shell_assets_and_rejects_asset_traversal(self):
        from robert_agent.interfaces.web import server as web
        from robert_agent.adapters.sqlite import database as storage

        db_path = self.root / "board-shell.sqlite3"
        storage.init_database(db_path)

        status, headers, body = web.build_http_response("/board", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        for text in (
            b"Backlog",
            b"TODO",
            b"Doing",
            b"Waiting for you",
            b"Review",
            b"Done",
            b"New task",
            b"Operations",
        ):
            self.assertIn(text, body)
        self.assertIn(b'id="mobile-column-tabs"', body)
        self.assertIn(b'id="work-item-drawer"', body)
        self.assertIn(b'aria-live="polite"', body)

        css_status, css_headers, css_body = web.build_http_response("/assets/board.css", db_path)
        js_status, js_headers, js_body = web.build_http_response("/assets/board.js", db_path)
        self.assertEqual(css_status, 200)
        self.assertEqual(css_headers["content-type"], "text/css; charset=utf-8")
        self.assertIn(b"@media", css_body)
        self.assertIn(b"[hidden]", css_body)
        body_rules = css_body.decode("utf-8").split("body {", 1)[1].split("}", 1)[0]
        self.assertIn("height: 100dvh;", {line.strip() for line in body_rules.splitlines()})
        self.assertEqual(js_status, 200)
        self.assertEqual(js_headers["content-type"], "text/javascript; charset=utf-8")
        self.assertIn(b"/api/board", js_body)
        favicon_status, _headers, favicon_body = web.build_http_response("/favicon.ico", db_path)
        self.assertEqual(favicon_status, 200)
        self.assertIn(b"<svg", favicon_body)

        traversal_status, _headers, _body = web.build_http_response("/assets/../web.py", db_path)
        self.assertEqual(traversal_status, 404)

    def test_web_serves_github_native_workbench_assets(self):
        from robert_agent.interfaces.web import server as web

        db_path = self.root / "dd.sqlite3"
        self._init_summary_db(db_path)

        status, headers, body = web.build_http_response("/work", db_path)
        html = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn('id="workbench-app"', html)
        self.assertIn('class="underline-nav"', html)
        self.assertIn('id="work-list"', html)
        self.assertIn('id="detail-panel"', html)
        self.assertIn("/assets/github-shell.css", html)
        self.assertIn("/assets/workbench.css", html)
        self.assertIn("/assets/workbench.js", html)
        self.assertIn('id="language-select"', html)
        self.assertNotIn('id="global-search"', html)
        self.assertNotIn("bootstrap", html.lower())

        status, headers, body = web.build_http_response("/assets/workbench.css", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/css; charset=utf-8")
        self.assertIn(b".workbench-shell", body)

        status, headers, body = web.build_http_response("/assets/github-shell.css", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/css; charset=utf-8")
        self.assertIn(b"--bgColor-default", body)

        status, headers, body = web.build_http_response("/assets/workbench.js", db_path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/javascript; charset=utf-8")
        self.assertIn(b"function loadWorkItems", body)
        self.assertIn(b"const translations", body)
        self.assertIn(b"function setLanguage", body)
        self.assertNotIn(b"/data.json", body)
