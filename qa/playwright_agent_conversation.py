"""Real Chromium regression for the multiline and expandable Agent conversation UI.

Uses the existing isolated in-memory API fixture. It makes no production,
OAuth, GitHub, or paid-model calls.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect

sys.path.insert(0, str(Path(__file__).resolve().parent))
import playwright_final_fix as fixture

ART = Path("qa/playwright_artifacts/agent-conversation")
ART.mkdir(parents=True, exist_ok=True)

DEMO_PROMPT = """Use GitHub MCP to inspect the bound Magic-Dala/archbro repository on the dev2 branch.

Verify whether the current Archbro Architecture matches the actual implementation.

Focus only on these major runtime boundaries:
- Web frontend and workspace UI
- Backend project / architecture / task domain
- Built-in Strands agent runtime
- Project-scoped MCP and provider integrations
- GitHub repository binding and evidence flow
- Persistence
- Deployment/runtime

For anything that does not match the current Architecture, propose the smallest architecture changes needed.

Do not modify code."""

RICH_RESPONSE = """## Architecture verification

- Web workspace: **VERIFIED**
- Agent runtime: **VERIFIED**
- Deployment/runtime: **UNVERIFIED**

| Boundary | Status | Evidence |
| --- | --- | --- |
| MCP | PARTIAL | `src/archbro/backend/mcp` |

Evidence: GitHub MCP read `Magic-Dala/archbro` on `dev2`.

1. **Web frontend**: PARTIAL
   - Evidence: `frontend/`

2. **Backend domain**: PARTIAL
   - Evidence: `src/`

3. **Persistence**: UNVERIFIED
   - Not inspected.

<script>unsafe()</script>"""


class ConversationBackend(fixture.FakeBackend):
    def __init__(self):
        super().__init__([fixture.project("A", "Archbro Demo")])
        self.latest_agent_run = None

    def handle(self, route):
        request = route.request
        method = request.method
        path = urlsplit(request.url).path
        parts = [part for part in path.split("/") if part]
        if parts[:2] == ["projects", "A"] and parts[2:] == ["events"] and method == "POST":
            body = request.post_data_json
            self.event_requests.append({"path": path, "body": copy.deepcopy(body)})
            self.latest_agent_run = {
                "result": "SUCCESS",
                "summary": RICH_RESPONSE,
                "provider": "fixture",
                "model": "fixture-model",
                "actions": [],
                "architecture_review_required": False,
                "evaluation": {
                    "evidence": [
                        "GitHub MCP get_file_contents: Magic-Dala/archbro ref=dev2 path=README.md"
                    ]
                },
                "error": None,
            }
            self.json(route, self.latest_agent_run)
            return
        if parts[:2] == ["projects", "A"] and parts[2:] == ["workspace-bootstrap"] and method == "GET":
            context = self.contexts["A"]
            version = int(context["architecture"].get("version", 0))
            self.json(route, {
                "schema": "archbro.workspace-bootstrap.v2",
                "project": copy.deepcopy(context["project"]),
                "tasks": copy.deepcopy(context["tasks"]),
                "architecture": copy.deepcopy(context["architecture"]),
                "proposals": copy.deepcopy(context["proposals"]),
                "activity": [],
                "latest_agent_run": copy.deepcopy(self.latest_agent_run),
                "resources": {
                    "canvas": {
                        "status": "DEFERRED",
                        "href": f"/projects/A/architecture/canvas?expected_architecture_version={version}&reading_mode=FULL",
                    },
                    "project_diagram": {
                        "status": "DEFERRED",
                        "href": f"/projects/A/architecture/diagram?expected_architecture_version={version}&reading_mode=MAP",
                    },
                },
                "built_in_model_called": False,
            })
            return
        super().handle(route)


def test_multiline_prompt_expand_keyboard_and_rich_response():
    def case(browser):
        backend = ConversationBackend()
        context, page, errors = fixture.open_page(
            browser,
            backend,
            identity="email:conversation@example.test",
            project_id="A",
        )
        page.set_default_timeout(6000)
        try:
            composer = page.locator("#instruction")
            expect(composer).to_be_visible()
            assert composer.evaluate("node => node.tagName") == "TEXTAREA"

            composer.fill(DEMO_PROMPT)
            assert composer.input_value() == DEMO_PROMPT
            assert composer.get_attribute("rows") == "8"
            assert "instruction-scrollable" in (composer.get_attribute("class") or "")
            assert backend.event_requests == []

            page.locator("[data-expand-agent-conversation]").first.click()
            dialog = page.locator("#agentConversationDialog")
            expect(dialog).to_be_visible()
            assert dialog.locator("#agentConversationPrompt").inner_text() == DEMO_PROMPT
            dialog.locator('[data-close-dialog="agentConversationDialog"]').last.click()
            expect(dialog).to_be_hidden()
            # Let the dialog's queued return-focus callback settle before testing
            # the independent composer keyboard contract.
            page.wait_for_timeout(100)

            composer.press("Enter")
            assert composer.input_value().endswith("\n")
            assert backend.event_requests == []
            composer.click()
            page.wait_for_timeout(20)
            page.keyboard.down("Control")
            page.keyboard.press("Enter")
            page.keyboard.up("Control")
            expect(page.locator("#globalAgentReply")).to_be_visible()
            assert len(backend.event_requests) == 1
            sent = backend.event_requests[0]["body"]["payload"]["message"]
            assert sent == DEMO_PROMPT

            page.locator("#globalAgentReply [data-expand-agent-conversation]").click()
            expect(dialog).to_be_visible()
            assert dialog.locator("#agentConversationPrompt").inner_text() == DEMO_PROMPT
            rich = dialog.locator("#agentConversationResponse")
            expect(rich.locator("h3")).to_have_text("Architecture verification")
            expect(rich.locator("ul").first.locator("li")).to_have_count(3)
            expect(rich.locator("table tbody tr")).to_have_count(1)
            expect(rich.locator(".agent-evidence-reference").first).to_contain_text("GitHub MCP")
            expect(rich).to_contain_text("GitHub MCP get_file_contents")
            assert "<script>" not in rich.inner_html()
            expect(rich).to_contain_text("<script>unsafe()</script>")
            for container in [rich, page.locator("#globalAgentReply")]:
                numbered = container.locator("ol > li")
                expect(numbered).to_have_count(3)
                assert numbered.evaluate_all("items => items.map(item => item.value)") == [1, 2, 3]
                expect(numbered.nth(1).locator("ul > li")).to_contain_text("src/")
            page.screenshot(path=str(ART / "expanded-conversation.png"), full_page=True)
            assert not errors, errors
        finally:
            context.close()

    fixture.run_case_with_static_server(case)


def test_wrapped_draft_keeps_whitespace_and_clears_on_project_switch():
    def case(browser):
        backend = fixture.FakeBackend([fixture.project('A', 'Alpha'), fixture.project('B', 'Beta')])
        context, page, errors = fixture.open_page(browser, backend, identity='email:conversation-scope@example.test', project_id='A', viewport={'width': 800, 'height': 900})
        try:
            composer = page.locator('#instruction')
            draft = '  ' + ('保留換行和空白 README.md / runtime ' * 60) + '\n  - indented list\n'
            composer.fill(draft)
            assert composer.input_value() == draft
            assert composer.get_attribute('rows') == '8'
            assert composer.evaluate('el => el.scrollHeight > el.clientHeight && getComputedStyle(el).overflowY === "auto"')
            page.locator('[data-expand-agent-conversation]').first.click()
            dialog = page.locator('#agentConversationDialog')
            assert dialog.locator('#agentConversationPrompt').text_content() == draft
            dialog.locator('[data-close-dialog="agentConversationDialog"]').last.click()
            row = page.locator('[data-project-id="B"]')
            row.locator('[data-project-toggle]').click()
            row.locator('[data-project-view="tasks"]').click()
            expect(composer).to_have_value('')
            page.locator('[data-expand-agent-conversation]').first.click()
            expect(dialog.locator('#agentConversationPrompt')).to_have_text('No instruction yet.')
            assert backend.event_requests == []
            assert not errors, errors
        finally:
            context.close()
    fixture.run_case_with_static_server(case)


def test_duplicate_shortcut_and_late_reply_do_not_clear_another_project_draft():
    def case(browser):
        backend = fixture.FakeBackend([fixture.project('A', 'Alpha'), fixture.project('B', 'Beta')])
        context, page, errors = fixture.open_page(browser, backend, identity='email:conversation-late@example.test', project_id='A')
        try:
            held = []
            page.route('**/projects/A/events', lambda route: held.append(route))
            composer = page.locator('#instruction')
            composer.fill('Keep this draft')
            page.keyboard.down('Control')
            page.keyboard.press('Enter')
            page.keyboard.press('Enter')
            page.keyboard.up('Control')
            page.wait_for_timeout(100)
            assert len(held) == 1
            row = page.locator('[data-project-id="B"]')
            row.locator('[data-project-toggle]').click()
            row.locator('[data-project-view="tasks"]').click()
            expect(composer).to_have_value('')
            composer.fill('Keep this draft')
            backend.json(held.pop(), {'result': 'SUCCESS', 'summary': 'PRIVATE_ALPHA_REPLY', 'provider': 'fixture', 'model': 'fixture', 'actions': [], 'architecture_review_required': False})
            page.wait_for_timeout(150)
            expect(composer).to_have_value('Keep this draft')
            page.locator('[data-expand-agent-conversation]').first.click()
            expect(page.locator('#agentConversationResponse')).not_to_contain_text('PRIVATE_ALPHA_REPLY')
            assert not errors, errors
        finally:
            context.close()
    fixture.run_case_with_static_server(case)


# Reuse the repository's disposable, schema-isolated PostgreSQL fixtures for the
# complete browser chain. The external model and GitHub gateway remain fixtures.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from conftest import dsn, repo, requires_database
from test_agent_evidence_acceptance import evidence_acceptance_client, ARCHITECTURE_PROMPT, PROGRESS_PROMPT


@requires_database
def test_evidence_to_review_accept_graph_and_progress_in_chromium(repo):
    with evidence_acceptance_client(repo) as (client, pid, gateway, registry, invocations):
        class ApiBridge:
            def __init__(self):
                self.requests = []

            def handle(self, route):
                request = route.request
                url = urlsplit(request.url)
                target = url.path + ('?' + url.query if url.query else '')
                response = client.request(request.method, target, content=request.post_data, headers={'Content-Type': 'application/json'})
                self.requests.append((request.method, target, response.status_code))
                route.fulfill(status=response.status_code, content_type='application/json', body=response.text)

        bridge = ApiBridge()

        def case(browser):
            context, page, errors = fixture.open_page(browser, bridge, identity='email:evidence-browser@example.test', project_id=pid)
            page.set_default_timeout(10000)
            try:
                page.locator(f'[data-project-id="{pid}"] [data-project-view="tasks"]').click()
                composer = page.locator('#instruction')
                composer.fill(ARCHITECTURE_PROMPT)
                page.locator('#instructionForm button[type="submit"]').click()
                expect(page.locator('#globalAgentReply')).to_contain_text('Persistence is PARTIAL')
                page.locator('#workspaceTabReview').click()
                expect(page.locator('[data-preview-version="1"]')).to_be_visible()
                assert repo.load_context(pid).architecture.version == 1
                pending, = repo.list_proposals(pid)
                assert pending.status == 'PENDING'
                page.locator('[data-proposal-decision="accept"]').click()
                expect(page.locator('.status-pill.ACCEPTED')).to_be_visible()
                assert repo.load_context(pid).architecture.version == 2
                row = page.locator(f'[data-project-id="{pid}"]')
                row.locator('[data-project-view="architecture"]').click()
                expect(page.locator('#graphCanvas svg')).to_be_visible()
                expect(page.locator('#graphCanvas')).to_contain_text('PostgreSQL')
                page.screenshot(path=str(ART / 'accepted-evidence-architecture.png'), full_page=True)
                row.locator('[data-project-view="tasks"]').click()
                composer.fill(PROGRESS_PROMPT)
                page.locator('#instructionForm button[type="submit"]').click()
                expect(page.locator('#globalAgentReply')).to_contain_text('Persistence: VERIFIED')
                expect(page.locator('#globalAgentReply')).to_contain_text('UNVERIFIED')
                assert len(gateway.calls) == 4
                assert invocations == [(True, False), (False, True)] * 2
                assert len([r for r in bridge.requests if r[0] == 'POST' and r[1].endswith('/accept')]) == 1
                assert all(status < 500 for _, _, status in bridge.requests)
                assert not errors, errors
            finally:
                context.close()
        fixture.run_case_with_static_server(case)
