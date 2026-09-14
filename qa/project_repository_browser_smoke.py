"""Disposable local browser acceptance for repository selection (no real provider calls)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'),str(ROOT/'tests')]

import psycopg
from psycopg.conninfo import make_conninfo, conninfo_to_dict
from playwright.sync_api import sync_playwright
import uvicorn

from archbro.backend.api.provider_connections import ProviderMcpRuntimeRegistry
from archbro.backend.core.contracts import Project, Architecture
from archbro.backend.llm.fake import FakeModelProvider
from archbro.platform.persistence.postgres import PostgresProjectRepository
from archbro.platform.runtime.app import create_app
from test_project_repository_api import RepositoryGateway
from test_agent_mcp_tools import PRIVATE_MARKER


def main():
    database=os.environ.get('DATABASE_URL','')
    info=conninfo_to_dict(database)
    if info.get('host') not in {'127.0.0.1','localhost'} or info.get('port')!='55479':
        raise RuntimeError('This smoke only runs against the disposable localhost:55479 QA database')
    schema='browser_repo_'+uuid4().hex
    with psycopg.connect(database,autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    server=None
    output=ROOT/'.local'/'browser-repository'
    output.mkdir(parents=True,exist_ok=True)
    try:
        repo=PostgresProjectRepository(make_conninfo(database,options=f'-c search_path={schema}'))
        projects=[]
        for name in ['Repository menu test','Independent project']:
            p=Project(name=name,goal='Keep project architecture aligned',owner_user_id='local-demo')
            repo.save_project(p);repo.save_architecture(p.id,Architecture());projects.append(p)
        registry=ProviderMcpRuntimeRegistry();gateway=RepositoryGateway()
        registry.gateways['local-demo']=gateway;registry.oauth_managers['local-demo']=object()
        with patch.dict(os.environ,{'ARCHBRO_ENV':'test','ARCHBRO_AUTH_MODE':'local','ARCHBRO_EDGE_GUARD':'off'}), patch('archbro.backend.api.routes.ProviderMcpRuntimeRegistry',return_value=registry):
            app=create_app(repo,FakeModelProvider(),web_dir=ROOT/'frontend'/'web')
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='warning'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        deadline=time.monotonic()+5
        while not server.started and time.monotonic()<deadline:
            time.sleep(.05)
        assert server.started
        origin=f'http://127.0.0.1:{port}'
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.set_default_timeout(12000)
            errors=[];page.on('pageerror',lambda exc:errors.append(str(exc)))
            page.add_init_script('''
                const session={id:'qa-local',provider:'password',email:'qa@example.test',name:'QA'};
                localStorage.setItem('archbro-demo-session',JSON.stringify(session));
                localStorage.setItem('archbro-demo-profiles',JSON.stringify({'qa-local':{...session,onboardingComplete:true,defaultLens:'software'}}));
            ''')
            page.goto(f'{origin}/?project={projects[0].id}')
            row=page.locator(f'[data-project-id="{projects[0].id}"]')
            row.locator('[data-project-menu]').click()
            row.locator('[data-project-action="repository"]').click()
            page.wait_for_function("() => document.getElementById('projectRepositoryName').disabled === false")
            page.locator('#projectRepositoryName').fill('archbro')
            page.locator('#projectRepositorySearch').click()
            page.locator('.repository-option').click()
            page.locator('#projectRepositoryBranch').fill('dev2')
            page.locator('#projectRepositorySave').click()
            page.wait_for_function("() => !document.getElementById('projectRepositoryDialog').open")
            assert repo.get_project(projects[0].id).source_repository.full_name=='Magic-Dala/archbro'
            row.locator('[data-project-menu]').click();row.locator('[data-project-action="repository"]').click()
            page.wait_for_function("() => document.getElementById('projectRepositoryName').value === 'Magic-Dala/archbro'")
            page.screenshot(path=str(output/'repository-menu-dialog.png'))
            box=page.locator('#projectRepositoryDialog').bounding_box()
            assert box and box['x']>=0 and box['y']>=0 and box['x']+box['width']<=1440
            page.locator('#projectRepositoryDialog [aria-label="Close"]').click()
            result=page.evaluate('''async () => {
                const {createArchBroTools}=await import('/static/archbro-webmcp.js');
                const tool=createArchBroTools(window.ArchBroWebBridge).find(t=>t.name==='archbro_call_connected_mcp_tool');
                return JSON.parse(await tool.execute({server_id:'mcp_private',tool_name:'get_file_contents',arguments:{path:'README.md'}}));
            }''')
            assert PRIVATE_MARKER in json.dumps(result)
            count=len(gateway.calls)
            denied=page.evaluate('''async () => {
                const {createArchBroTools}=await import('/static/archbro-webmcp.js');
                const tool=createArchBroTools(window.ArchBroWebBridge).find(t=>t.name==='archbro_call_connected_mcp_tool');
                try {await tool.execute({server_id:'mcp_private',tool_name:'get_file_contents',arguments:{owner:'evil',repo:'other',path:'README.md'}});return false;} catch {return true;}
            }''')
            assert denied and len(gateway.calls)==count
            page.goto(f'{origin}/?project={projects[1].id}')
            row=page.locator(f'[data-project-id="{projects[1].id}"]')
            row.locator('[data-project-menu]').click();row.locator('[data-project-action="repository"]').click()
            page.wait_for_function("() => document.getElementById('projectRepositoryName').disabled === false")
            assert page.locator('#projectRepositoryName').input_value()==''
            assert repo.get_project(projects[1].id).source_repository is None
            assert not errors,errors
            report={'status':'PASS','classification':'LOCAL_BROWSER_REAL_API_FAKE_GITHUB',
                    'sidebar_menu':True,'search_select_save_reload':True,'cross_project_isolation':True,
                    'webmcp_project_route_read':True,'foreign_repo_blocked_before_dispatch':True,
                    'page_errors':errors,'real_github_or_paid_model_calls':0,'screenshot':str(output/'repository-menu-dialog.png')}
            (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report))
            browser.close()
    finally:
        if server:
            server.should_exit=True
            thread.join(timeout=5)
        with psycopg.connect(database,autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA "{schema}" CASCADE')

if __name__=='__main__':
    main()
