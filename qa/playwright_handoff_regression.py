"""Handoff regression browser tests. Real browser/UI, isolated in-memory API fixtures.

Run: python -m pytest qa/playwright_handoff_regression.py -q
No production requests, real OAuth, repository binding changes or paid model calls.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from playwright.sync_api import expect
sys.path.insert(0, str(Path(__file__).resolve().parent))
import playwright_final_fix as fixture

ART = Path('qa/playwright_artifacts/handoff')
ART.mkdir(parents=True, exist_ok=True)


def backend():
    value = fixture.FakeBackend([fixture.project('A','Alpha'), fixture.project('B','Beta')])
    for pid in ('A','B'):
        value.contexts[pid]['tasks'] = [fixture.task(pid, f'{pid}-task', f'{pid} fixture task', 'TODO')]
        value.contexts[pid]['proposals'] = [fixture.proposal(pid, f'{pid}-proposal')]
    return value


def open_fixture(browser, value=None):
    value = value if value is not None else backend()
    context,page,errors = fixture.open_page(browser, value,
        identity='email:handoff@example.test', project_id='A' if value.projects else None)
    page.set_default_timeout(6000)
    return context,page,errors


def child(page,pid,view):
    row=page.locator(f'[data-project-id="{pid}"]')
    button=row.locator(f'[data-project-view="{view}"]')
    if not button.is_visible():
        row.locator('[data-project-toggle]').click()
    button.click()


def assert_map(page,pid):
    expect(page.locator('#overviewArchitectureMap svg')).to_be_visible()
    assert pid in page.locator('#overviewArchitectureMap').inner_html()
    assert 'Loading map' not in page.locator('#overviewArchitectureMap').inner_text()


@pytest.mark.parametrize('target',['A','B'])
def test_delayed_map_survives_history(target):
    def case(browser):
        value=backend(); context,page,errors=open_fixture(browser,value)
        try:
            other='B' if target=='A' else 'A'
            child(page,other,'overview'); assert_map(page,other)
            held=[]
            page.route(f'**/projects/{target}/architecture/diagram?**', lambda route: held.append(route))
            child(page,target,'overview')
            page.wait_for_timeout(100)
            assert len(held)==1, f'expected one paused map, got {len(held)}'
            child(page,target,'tasks')
            child(page,target,'architecture')
            page.go_back(); expect(page.locator('#pageTitle')).to_have_text('Tasks')
            page.go_forward(); expect(page.locator('#pageTitle')).to_have_text('Living Architecture')
            value.json(held.pop(),value.scoped_diagram_payload(target))
            page.unroute(f'**/projects/{target}/architecture/diagram?**')
            expect(page.locator('#graphCanvas svg')).to_be_visible()
            child(page,target,'overview'); assert_map(page,target)
            page.screenshot(path=str(ART/f'history-{target}.png'))
            assert not errors, errors
        finally:
            context.close()
    fixture.run_case_with_static_server(case)


@pytest.mark.parametrize('late,current',[('A','B'),('B','A')])
def test_cross_project_late_map_cannot_overwrite(late,current):
    def case(browser):
        value=backend(); context,page,errors=open_fixture(browser,value)
        try:
            child(page,current,'overview'); assert_map(page,current)
            held=[]
            page.route(f'**/projects/{late}/architecture/diagram?**',lambda route:held.append(route))
            child(page,late,'overview'); page.wait_for_timeout(100)
            assert held
            child(page,current,'overview'); assert_map(page,current)
            for route in held: value.json(route,value.scoped_diagram_payload(late))
            page.unroute(f'**/projects/{late}/architecture/diagram?**')
            page.wait_for_timeout(150); assert_map(page,current)
            assert not errors, errors
        finally: context.close()
    fixture.run_case_with_static_server(case)


def test_explicit_tasks_sidebar_and_deep_link_override_review():
    def case(browser):
        context,page,errors=open_fixture(browser)
        try:
            child(page,'A','tasks'); page.locator('#workspaceTabReview').click()
            expect(page.locator('#pageTitle')).to_have_text('Architecture Review')
            child(page,'A','overview'); child(page,'A','tasks')
            expect(page.locator('#pageTitle')).to_have_text('Tasks')
            child(page,'B','tasks'); page.locator('#workspaceTabReview').click()
            child(page,'A','overview'); child(page,'B','tasks')
            expect(page.locator('#pageTitle')).to_have_text('Tasks')
            page.evaluate("localStorage.setItem('archbro-workspace-tab:A','review')")
            page.goto(fixture.BASE_URL+'?project=A&view=tasks',wait_until='networkidle')
            expect(page.locator('#pageTitle')).to_have_text('Tasks')
            page.goto(fixture.BASE_URL+'?project=A&view=tasks&workspace=review',wait_until='networkidle')
            expect(page.locator('#pageTitle')).to_have_text('Architecture Review')
            assert not errors, errors
        finally: context.close()
    fixture.run_case_with_static_server(case)


@pytest.mark.parametrize('view',['tasks','review','architecture'])
def test_home_hides_unowned_project_panels(view):
    def case(browser):
        context,page,errors=open_fixture(browser)
        try:
            child(page,'A','tasks' if view=='review' else view)
            if view=='review': page.locator('#workspaceTabReview').click()
            page.locator('#workspaceSwitcherBtn').click()
            expect(page.locator('#pageTitle')).to_have_text('Project workspace')
            expect(page.locator('#workspaceHome')).to_be_visible()
            expect(page.locator('#workspaceTabs')).to_be_hidden()
            for selector in ('#workspaceTabTasksPanel','#workspaceTabReviewPanel','#view-architecture','#taskDetailPanel'):
                expect(page.locator(selector)).to_be_hidden()
            assert 'personal workspace' not in page.locator('body').inner_text().lower()
            assert 'A fixture task' not in page.locator('body').inner_text()
            page.reload(wait_until='networkidle')
            expect(page.locator('#workspaceHome')).to_be_visible()
            assert 'personal workspace' not in page.locator('body').inner_text().lower()
            assert not errors, errors
        finally: context.close()
    fixture.run_case_with_static_server(case)


def test_home_responsive_normal_empty_and_keyboard():
    def case(browser):
        measurements=[]
        for empty in (False,True):
            value=fixture.FakeBackend([]) if empty else backend()
            context,page,errors=open_fixture(browser,value)
            try:
                if not empty: page.locator('#workspaceSwitcherBtn').click()
                expect(page.locator('#workspaceHome')).to_be_visible()
                for width in (390,761,860,900,1050,1051,1440):
                    page.set_viewport_size({'width':width,'height':900})
                    page.wait_for_timeout(60)
                    metrics=page.locator('#pageTitle').evaluate("""node=>{
                      const r=document.createRange();r.selectNodeContents(node);
                      const rects=[...r.getClientRects()].filter(x=>x.width>0);
                      return {lines:new Set(rects.map(x=>Math.round(x.top))).size,
                        textWidth:r.getBoundingClientRect().width, width:node.clientWidth,
                        overflow:document.documentElement.scrollWidth>innerWidth};
                    }""")
                    measurements.append({'width':width,'empty':empty,**metrics})
                    assert metrics['lines']==1, measurements[-1]
                    assert metrics['textWidth']<=metrics['width']+1, measurements[-1]
                    assert not metrics['overflow'], measurements[-1]
                    assert 'personal workspace' not in page.locator('body').inner_text().lower()
                    if width==390:
                        page.screenshot(path=str(ART/f'home-390-{empty}.png'))
                        page.locator('#mobileSidebarBtn').click()
                        switcher=page.get_by_role('button',name='Open Project workspace',exact=True)
                        expect(switcher).to_be_visible(); switcher.focus(); page.keyboard.press('Enter')
                        expect(page.locator('#pageTitle')).to_have_text('Project workspace')
                assert not errors, errors
            finally: context.close()
        (ART/'responsive.json').write_text(json.dumps(measurements,indent=2),encoding='utf-8')
    fixture.run_case_with_static_server(case)


def test_oauth_completion_immediately_updates_connected_ui():
    def case(browser):
        context,page,errors=open_fixture(browser)
        connected={'value':False}
        conn={'id':'fixture-github','provider':'github','name':'GitHub','transport':'http',
              'endpoint':'GitHub remote MCP','last_probe_ok':True,'tool_count':11,
              'persistent':True,'auth_type':'oauth','authorization_pending':False}
        def mcp(route):
            path=urlsplit(route.request.url).path
            if path=='/mcp/connections': body=[conn] if connected['value'] else []
            elif path.endswith('/status'):
                body={'provider':'github','configured':True,'supported':True,'connected':connected['value'],
                      'persistent':connected['value'],'storage':'encrypted_postgres','connection':conn if connected['value'] else None}
            else: body={}
            route.fulfill(status=200,content_type='application/json',body=json.dumps(body))
        page.route('**/mcp/**',mcp)
        try:
            page.locator('#accountBtn').click(); page.locator('#accountMcpConnectionsBtn').click()
            expect(page.locator('#mcpConnectionsDialog')).to_be_visible()
            connected['value']=True
            page.evaluate("window.postMessage({type:'archbro-mcp-oauth',provider:'github',ok:true,message:'GitHub connected.'},location.origin)")
            expect(page.locator('#mcpConnectionNotice')).to_be_visible()
            expect(page.locator('#mcpConnectionList')).to_contain_text('GitHub')
            expect(page.locator('#mcpConnectionList')).to_contain_text('Saved securely')
            page.screenshot(path=str(ART/'oauth-completed.png'))
            assert not errors, errors
        finally: context.close()
    fixture.run_case_with_static_server(case)


def test_last_fixture_project_deletion_leaves_clean_empty_home():
    def case(browser):
        value=fixture.FakeBackend([fixture.project('A','Alpha')])
        value.contexts['A']['tasks']=[fixture.task('A','A-task','Deleted fixture task','TODO')]
        context,page,errors=open_fixture(browser,value)
        deletes=[]
        def delete_fixture(route):
            if route.request.method != 'DELETE':
                route.fallback(); return
            deletes.append(route.request.url)
            value.projects=[]; value.contexts.clear()
            route.fulfill(status=204,body='')
        page.route('**/projects/A',delete_fixture)
        try:
            child(page,'A','tasks')
            page.locator('[data-project-id="A"] [data-project-menu]').click()
            page.locator('[data-project-id="A"] [data-project-action="delete"]').click()
            dialog=page.locator('#deleteProjectDialog')
            expect(dialog).to_be_visible()
            dialog.locator('button[type="submit"]').click()
            expect(page.locator('#workspaceHomeEmpty')).to_be_visible()
            expect(page.locator('#workspaceTabTasksPanel')).to_be_hidden()
            assert len(deletes)==1
            assert 'Deleted fixture task' not in page.locator('body').inner_text()
            page.reload(wait_until='networkidle')
            expect(page.locator('#workspaceHomeEmpty')).to_be_visible()
            assert not errors, errors
        finally: context.close()
    fixture.run_case_with_static_server(case)
