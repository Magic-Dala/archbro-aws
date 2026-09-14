"""Offline visual checks of the actual Canvas renderer, without auth or a server.

Only the test-served module replaces app startup with fixed accepted data.
Authentication entry points throw, all requests are fulfilled from local assets
or blocked. This is visual/unit evidence, never real-auth/provider acceptance.
"""
import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright


async def verify_connection_domains(page):
    result=await page.evaluate('''() => {
      const diagram=__canvasTest.state.diagram;
      const selected=diagram.nodes.find(n=>n.component_id===__canvasTest.state.selectedComponentId);
      const faults=[], checked=[];
      for(const group of document.querySelectorAll('svg [data-connection-domain]')) {
        if(!group.dataset.connectionDomain) continue;
        const ids=JSON.parse(group.dataset.memberEdges);
        for(const id of ids) {
          const edge=diagram.edges.find(e=>e.id===id);
          let peer=diagram.nodes.find(n=>n.id===(edge.source===selected.id?edge.target:edge.source));
          while(peer.parent_id) peer=diagram.nodes.find(n=>n.id===peer.parent_id);
          const frame=document.querySelector(`[data-group-node="${CSS.escape(peer.id)}"] rect`);
          const color=getComputedStyle(frame).stroke;
          const badge=document.querySelector(`#selectedNode [data-related-edge="${CSS.escape(id)}"] [data-peer-domain]`);
          if(group.dataset.connectionDomain!==peer.id || !badge || badge.dataset.peerDomain!==peer.id ||
            !badge.textContent.includes(peer.label) || getComputedStyle(badge.querySelector('i')).backgroundColor!==color ||
            [...group.querySelectorAll('.graph-edge-line,.graph-edge-arrow')].some(el=>getComputedStyle(el).stroke!==color)) faults.push(id);
          checked.push(id);
        }
      }
      return {checked:checked.length,faults};
    }''')
    assert result['checked'] and not result['faults'],result
    return result


async def verify_connection_inspection(page):
    result=await page.evaluate("""() => {
      const api=window.__canvasTest, svg=document.querySelector('.living-graph-svg');
      const geometry=()=>JSON.stringify([...svg.querySelectorAll('[data-edge]')].map(el=>[el.dataset.memberEdges,el.querySelector('.graph-edge-line').getAttribute('d')]).sort());
      const initial=geometry(), accepted=api.canonical(), selection=api.state.selectedComponentId, viewport=svg.getAttribute('viewBox');
      const checked=[],faults=[];
      for(const el of [...svg.querySelectorAll('[data-edge]')]) {
        el.focus();
        const ids=JSON.parse(el.dataset.memberEdges), members=api.state.diagram.edges.filter(edge=>ids.includes(edge.id));
        const endpoints=[...new Set(members.flatMap(edge=>[edge.source,edge.target]))].sort();
        const lit=[...svg.querySelectorAll('[data-node].is-tracing-connection')].map(node=>node.dataset.node).sort();
        const readout=document.querySelector('.canvas-connection-readout');
        if(document.activeElement!==el || readout.hidden || JSON.stringify(endpoints)!==JSON.stringify(lit) || svg.querySelectorAll('.graph-edge.is-tracing-connection').length!==1) faults.push({ids,kind:'focus/endpoints'});
        for(const member of members) {
          const name=id=>api.state.diagram.nodes.find(node=>node.id===id)?.label || id;
          if(!readout.textContent.includes(name(member.source)+' → '+name(member.target)) || !readout.textContent.includes(member.label || member.semantic_type || '')) faults.push({ids,kind:'named directed facts'});
        }
        const copy=svg.querySelector('.canvas-trace-paint .canvas-trace-stroke');
        if(copy?.getAttribute('d')!==el.querySelector('.graph-edge-line').getAttribute('d')) faults.push({ids,kind:'route changed'});
        checked.push(...ids);el.blur();
      }
      const restored=document.querySelector('.canvas-connection-readout').hidden && !svg.classList.contains('is-tracing-connection') && !svg.querySelector('.canvas-trace-paint');
      return {checked:checked.length,visual:[...svg.querySelectorAll('[data-edge]')].length,faults,restored,unchanged:initial===geometry() && accepted===api.canonical() && selection===api.state.selectedComponentId && viewport===svg.getAttribute('viewBox')};
    }""")
    assert result['checked'] and not result['faults'] and result['restored'] and result['unchanged'],result
    return result


async def verify_connection_summaries(page, fixture, output):
    checks=[]
    for summary in fixture['canvas'].get('connection_summaries',{}).get('groups',[]):
        await page.locator('[data-canvas-reading-view]').select_option('backbone')
        await page.evaluate('(id)=>__canvasTest.select(__canvasTest.state.diagram.nodes.find(n=>n.id===id))',summary['hub_node_id'])
        await page.locator('[data-graph-viewport="fit"]').click()
        group=page.locator('svg [data-summary-id="'+summary['id']+'"]')
        assert await group.count()==1,summary
        assert json.loads(await group.get_attribute('data-member-edges'))==summary['member_edge_ids']
        assert await group.locator('.graph-edge-arrow').count()==(1 if summary['direction']=='IN' else len(summary['member_edge_ids']))
        for member_id in summary['member_edge_ids']:
            row=page.locator('#selectedNode [data-related-edge="'+member_id+'"]')
            await row.hover()
            result=await page.evaluate('''(id)=>{
              const fact=__canvasTest.state.diagram.edges.find(e=>e.id===id);
              const names=[fact.source,fact.target].map(id=>__canvasTest.state.diagram.nodes.find(n=>n.id===id).label);
              const readout=document.querySelector('.canvas-connection-readout');
              const endpoints=[...document.querySelectorAll('svg [data-node].is-tracing-connection')].map(el=>el.dataset.node).sort();
              return {readout:readout.textContent,names,expected:[fact.source,fact.target].sort(),endpoints,individual:!!document.querySelector('.graph-edge.is-tracing-individual')};
            }''',member_id)
            assert result['individual'] and result['endpoints']==result['expected'] and all(name in result['readout'] for name in result['names']),result
        await page.mouse.move(10,10)
        await group.focus()
        assert await page.locator('svg [data-node].is-tracing-connection').count()==len(summary['member_edge_ids'])+1
        assert await page.locator('.canvas-connection-readout>div').count()==len(summary['member_edge_ids'])
        viewport=await page.locator('.living-graph-svg').get_attribute('viewBox')
        if summary['hub_node_id']=='node:event_bus':
            await page.screenshot(path=str(output/'two-event-sources-highlight.png'))
        await group.press('Enter')
        assert await page.locator('[data-summary-id="'+summary['id']+'"]').count()==0
        for member_id in summary['member_edge_ids']:
            assert await page.locator('svg [data-edge="'+member_id+'"] .graph-edge-arrow').count()==1
        assert viewport==await page.locator('.living-graph-svg').get_attribute('viewBox')
        await page.locator('[data-regroup-connections]').click()
        assert await group.count()==1
        if summary['hub_node_id']=='node:event_bus':
            await page.mouse.move(10,10)
            await page.screenshot(path=str(output/'two-event-sources-grouped.png'))
        checks.append(dict(id=summary['id'],members=len(summary['member_edge_ids']),direction=summary['direction'],individual_sources=True,expand_regroup=True))
    # Exercise the actual fold affordance, not just the implementation helper.
    await page.locator('[data-canvas-reading-view]').select_option('all')
    await page.locator('[data-graph-viewport="fit"]').click()
    fold=page.locator('.canvas-group-fold').first
    owner=await fold.get_attribute('data-fold-group')
    before=await page.locator('svg [data-node]').count()
    await fold.click()
    assert await page.locator('.canvas-group-summary[data-fold-group="'+owner+'"]').count()==1
    assert await page.locator('svg [data-node]').count()<before
    await page.screenshot(path=str(output/(fixture['sample']['sample_id']+'-grouped-components.png')))
    await page.locator('.canvas-group-summary[data-fold-group="'+owner+'"]').click()
    assert await page.locator('svg [data-node]').count()==before
    return checks


async def run(args):
    web = args.repo/'frontend/web'
    candidate = args.candidate/'frontend/web' if args.candidate else web
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={'width':1426,'height':837}, device_scale_factor=1)
        for fixture_path in sorted(args.fixtures.glob('0*-canvas.json')):
            fixture=json.loads(fixture_path.read_text(encoding='utf-8'))
            sample=fixture['sample']
            page=await context.new_page()
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda msg:errors.append(msg.text) if msg.type=='error' else None)

            async def serve(route):
                url=urlsplit(route.request.url)
                if url.hostname!='archbro.canvas.test':
                    errors.append('Unexpected external request: '+url.hostname)
                    await route.abort(); return
                path=url.path
                if path=='/':
                    html=((candidate/'index.html') if (candidate/'index.html').is_file() else (web/'index.html')).read_text(encoding='utf-8')
                    html=re.sub(r'<script[^>]+(?:runtime-config|archbro-webmcp|firebase-auth-client)[^>]*></script>','',html)
                    await route.fulfill(body=html,content_type='text/html'); return
                if path.startswith('/static/'):
                    rel=path.removeprefix('/static/')
                    asset=candidate/rel
                    if not asset.is_file(): asset=web/rel
                    if not asset.is_file():
                        errors.append('Missing local asset: '+path)
                        await route.fulfill(status=404); return
                    body=asset.read_text(encoding='utf-8')
                    if rel=='app.js':
                        match=re.search(r"import\s*\{(.*?)\}\s*from './firebase-auth\.js[^']*';",body,re.S)
                        names=[name.strip() for name in match.group(1).split(',') if name.strip()]
                        disabled='\n'.join(f'const {name}=()=>{{throw new Error("Authentication is unavailable in offline renderer tests");}};' for name in names)
                        body=body[:match.start()]+disabled+body[match.end():]
                        body=body.replace('appInitializationPromise = initialize();','/* Test harness owns startup; no auth or API calls. */')
                        body+='\n'+'''
const visualFixture = '''+json.dumps(fixture,ensure_ascii=False)+''';
updateInstructionContext=()=>{};
state.projectId=visualFixture.sample.sample_id;
state.project={id:state.projectId,name:visualFixture.sample.payload.name,goal:visualFixture.sample.payload.goal};
state.architecture=visualFixture.architecture;
state.diagram=normalizeFullCanvasResponse(visualFixture.canvas);
state.tasks=[]; state.projects=[state.project]; state.currentView='architecture';
state.onboarding.active=false;
document.body.classList.add('architecture-canvas-mode');
$('workspaceShell').classList.remove('hidden');
$('workspace').classList.remove('hidden');
document.querySelectorAll('.view').forEach((el)=>el.classList.toggle('active',el.id==='view-architecture'));
document.querySelectorAll('body > section, #landingPage, #authPage, #onboardingPage').forEach((el)=>{if(el.id!=='workspaceShell')el.classList.add('hidden');});
window.__canvasTest={
  state, render:renderGraph, display:()=>graphDisplayModel(state.diagram),
  selectEdge:selectGraphEdge,
  select:activateGraphNode, toggle:toggleGraphNodeCollapse,
  viewport:setGraphViewportBox,
  canonical:()=>JSON.stringify({architecture:state.architecture,diagram:state.diagram}),
};
$('graphVersion').textContent='v'+state.architecture.version;
renderGraph();
'''
                    await route.fulfill(body=body,content_type='text/css' if rel.endswith('.css') else 'text/javascript');return
                errors.append('Unexpected request: '+path)
                await route.fulfill(status=404)

            await page.route('**/*',serve)
            await page.goto('http://archbro.canvas.test/?canvas=architecture&project='+sample['sample_id'])
            try:
                await page.wait_for_selector('.living-graph-svg',timeout=10000)
            except Exception:
                print(json.dumps({'sample':sample['sample_id'],'errors':errors}),flush=True)
                raise
            await page.evaluate('document.fonts.ready')
            await page.screenshot(path=str(args.output/(sample['sample_id']+'-overview.png')))
            meta=await page.evaluate('''() => ({nodes:document.querySelectorAll('svg [data-node]').length,edges:document.querySelectorAll('svg [data-edge]').length,meta:document.querySelector('.graph-meta').textContent,svg:document.querySelector('svg.living-graph-svg').getBoundingClientRect().toJSON(),zoom:document.querySelector('[data-graph-zoom]').textContent})''')
            before=await page.evaluate('window.__canvasTest.canonical()')
            if fixture['canvas'].get('presentation'):
                assert not re.search(r'[\u3400-\u9fff]',await page.locator('#view-architecture').inner_text()), 'Untranslated overview text'
            first=sample['scenarios'][0]
            await page.locator('[data-component="'+first['focus']+'"]').click()
            await page.screenshot(path=str(args.output/(sample['sample_id']+'-selected.png')))
            selected_edges=await page.locator('svg [data-edge]').count()
            if fixture['canvas'].get('presentation'):
                assert not re.search(r'[\u3400-\u9fff]',await page.locator('#selectedNode').inner_text()), 'Untranslated inspector text'
            direct_ids=await page.evaluate("""() => __canvasTest.state.diagram.edges.filter(e=>e.source==='node:'+__canvasTest.state.selectedComponentId || e.target==='node:'+__canvasTest.state.selectedComponentId).map(e=>e.id).sort()""")
            shown_ids=await page.locator('svg [data-edge]').evaluate_all('(els)=>els.flatMap(e=>JSON.parse(e.dataset.memberEdges || JSON.stringify([e.dataset.edge])))')
            assert set(direct_ids)<=set(shown_ids)
            inspector_ids=await page.locator('#selectedNode [data-related-edge]').evaluate_all('(els)=>els.map(e=>e.dataset.relatedEdge).sort()')
            assert direct_ids==inspector_ids
            domain_checks=[await verify_connection_domains(page)]
            await page.locator('#selectedNode [data-related-edge]').first.click()
            assert await page.locator('#selectedNode').inner_text()
            await page.locator('[data-canvas-reading-view]').select_option('all')
            assert await page.locator('svg [data-edge]').evaluate_all('(els)=>els.reduce((n,e)=>n+JSON.parse(e.dataset.memberEdges).length,0)')==len(fixture['canvas']['diagram']['edges'])
            inspection_checks=await verify_connection_inspection(page)
            summary_checks=await verify_connection_summaries(page,fixture,args.output)
            bundles=await page.locator('svg [data-edge]').evaluate_all('(els)=>els.map(e=>JSON.parse(e.dataset.memberEdges)).filter(ids=>ids.length===2)')
            if bundles:
                pair=bundles[0]
                if sample['sample_id']=='01-commerce':
                    await page.locator('[data-canvas-reading-view]').select_option('backbone')
                    await page.locator('[data-component="warehouse"]').click()
                    pair=await page.locator('svg [data-edge]').evaluate_all('(els)=>els.map(e=>JSON.parse(e.dataset.memberEdges)).find(ids=>ids.length===2)')
                    await page.screenshot(path=str(args.output/(sample['sample_id']+'-warehouse-two-way.png')))
                await page.locator('svg [data-edge="'+pair[0]+'"]').press('Enter')
                assert await page.locator('.connection-directions [data-related-edge]').count()==2
                for edge_id in pair:
                    await page.locator('.connection-directions [data-related-edge="'+edge_id+'"]').click()
                    assert await page.locator('svg .graph-edge.selected .graph-edge-arrow').count()==2
                    assert await page.locator('svg .graph-edge.selected').get_attribute('data-edge')==edge_id
                    assert edge_id in await page.locator('#nodeEvidence .inspector-facts').inner_text()
                await page.screenshot(path=str(args.output/(sample['sample_id']+'-two-way-inspector.png')))
            journeys=[]
            for index,scenario in enumerate(sample['scenarios'],1):
                await page.locator('[data-canvas-reading-view]').select_option(sample['sample_id']+':'+str(index))
                actual=await page.locator('svg [data-edge]').evaluate_all('(els)=>els.map(e=>e.dataset.edge).sort()')
                expected=fixture['canvas']['reading_views'][index]['edge_ids']
                assert actual==sorted(expected), (scenario['name'],actual,expected)
                assert await page.locator('svg .node-card.selected').count()==0
                journeys.append(dict(name=fixture['canvas'].get('presentation',{}).get('reading_labels',{}).get(sample['sample_id']+':'+str(index),scenario['name']),edges=len(actual)))
                await page.screenshot(path=str(args.output/(sample['sample_id']+'-journey-'+str(index)+'.png')))
                if fixture['canvas'].get('presentation'):
                    assert not re.search(r'[\u3400-\u9fff]',await page.locator('#view-architecture').inner_text()), 'Untranslated journey text'
                if index==1:
                    await page.screenshot(path=str(args.output/(sample['sample_id']+'-journey.png')))
            await page.locator('[data-canvas-reading-view]').select_option('backbone')
            if sample['sample_id']=='01-commerce':
                await page.locator('[data-component="event_bus"]').click()
                expected_hub=await page.evaluate("__canvasTest.state.diagram.edges.filter(e=>e.source==='node:event_bus' || e.target==='node:event_bus').map(e=>e.id)")
                shown_hub=await page.locator('svg [data-member-edges]').evaluate_all('(els)=>els.flatMap(e=>JSON.parse(e.dataset.memberEdges))')
                assert set(expected_hub)<=set(shown_hub)
                domain_checks.append(await verify_connection_domains(page))
                await page.mouse.move(10,10)
                await page.screenshot(path=str(args.output/(sample['sample_id']+'-event-bus.png')))
                # Real pointer hover on an Inspector row must trace the same
                # relationship, including when its endpoints are offscreen.
                hover_row=page.locator('#selectedNode [data-related-edge]').first
                hover_id=await hover_row.get_attribute('data-related-edge')
                await hover_row.hover()
                assert await page.locator('.canvas-connection-readout').is_visible()
                assert hover_id in await page.locator('svg .graph-edge.is-tracing-connection').get_attribute('data-member-edges')
                await page.screenshot(path=str(args.output/'event-bus-source-inspection.png'))
                await page.mouse.move(10,10)
                assert not await page.locator('.canvas-connection-readout').is_visible()
                await page.evaluate("""() => {
                  const svg=document.querySelector('.living-graph-svg'),r=svg.getBoundingClientRect(),n=__canvasTest.state.diagram.nodes.find(n=>n.component_id==='event_bus');
                  __canvasTest.viewport(svg,{x:n.x-230,y:n.y-170,width:r.width/1.4,height:r.height/1.4});
                }""")
                await page.screenshot(path=str(args.output/'event-bus-approach-lanes.png'))

                await page.locator('[data-graph-focus="clear"]').click()
            await page.locator('[data-graph-viewport="actual"]').click()
            overflow=await page.evaluate("""() => [...document.querySelectorAll('svg [data-node]')].flatMap(el=>{
              const node=__canvasTest.state.diagram.nodes.find(n=>n.id===el.dataset.node);
              return [...el.querySelectorAll('text')].filter(t=>getComputedStyle(t).display!=='none').flatMap(t=>{
                const b=t.getBBox(); return b.x<node.x || b.x+b.width>node.x+node.width+.5 || b.y<node.y || b.y+b.height>node.y+node.height+.5 ? [{id:node.id,text:t.textContent,box:{x:b.x,y:b.y,width:b.width,height:b.height}}] : [];
              });
            })""")
            assert not overflow, overflow
            await page.screenshot(path=str(args.output/(sample['sample_id']+'-actual.png')))
            arrow_checks=[]
            for scale in [1, 2, 4]:
                check=await page.evaluate('''({scale, focus}) => {
                  const svg=document.querySelector('svg.living-graph-svg');
                  const r=svg.getBoundingClientRect();
                  const n=__canvasTest.state.diagram.nodes.find(n=>n.component_id===focus);
                  __canvasTest.viewport(svg,{x:n.x+n.width/2-r.width/scale/2,
                    y:n.y+n.height/2-r.height/scale/2,width:r.width/scale,height:r.height/scale});
                  const arrows=[...svg.querySelectorAll('.graph-edge-arrow')];
                  const faults=arrows.flatMap(arrow=>{
                    const group=arrow.closest('[data-edge]');
                    const edge=__canvasTest.display().edges.find(e=>e.id===JSON.parse(group.dataset.memberEdges)[0]);
                    const summary=__canvasTest.state.diagram.connectionSummaries?.find(s=>s.id===group.dataset.summaryId);
                    const arrowIndex=[...group.querySelectorAll('.graph-edge-arrow')].indexOf(arrow);
                    const end=summary ? summary.paths[summary.direction==='IN'?0:arrowIndex].at(-1) : arrow.classList.contains('graph-edge-arrow-start') ? edge.points[0] : edge.points.at(-1);
                    const tip=new DOMPoint(0,0).matrixTransform(arrow.getScreenCTM());
                    const expected=new DOMPoint(end.x,end.y).matrixTransform(svg.getScreenCTM());
                    const bounds=arrow.getBoundingClientRect();
                    const lineStyle=getComputedStyle(group.querySelector('.graph-edge-line'));
                    return Math.hypot(tip.x-expected.x,tip.y-expected.y)>.01 ||
                      Math.abs(bounds.width-6)>.02 || Math.abs(bounds.height-6)>.02 ||
                      getComputedStyle(arrow).stroke!==lineStyle.stroke || lineStyle.strokeDasharray!=='none'
                      ? [group.dataset.edge] : [];
                  });
                  return {scale,arrows:arrows.length,edges:[...svg.querySelectorAll('[data-edge]')].reduce((n,e)=>{const summary=__canvasTest.state.diagram.connectionSummaries?.find(s=>s.id===e.dataset.summaryId);return n+(summary?.direction==='IN'?1:JSON.parse(e.dataset.memberEdges).length);},0),
                    sourceDots:svg.querySelectorAll('.graph-edge-source-port').length,faults};
                }''',dict(scale=scale,focus=first['focus']))
                assert check['arrows']==check['edges'] and check['sourceDots']==0 and not check['faults'],check
                arrow_checks.append(check)
                await page.screenshot(path=str(args.output/(sample['sample_id']+f'-connections-{scale}x.png')))
            original_display=await page.evaluate('JSON.stringify(__canvasTest.display())')
            group_ids=await page.evaluate('__canvasTest.state.diagram.nodes.filter(n=>n.childCount>0).map(n=>n.id)')
            for group in group_ids:
                await page.evaluate('(id)=>__canvasTest.toggle(id)',group)
                clipped=await page.evaluate("""() => {
                  const d=__canvasTest.display();
                  return d.edges.every(e=>e.points.slice(1).every((p,i)=>p.x===e.points[i].x || p.y===e.points[i].y));
                }""")
                assert clipped, group
                await page.evaluate('(id)=>__canvasTest.toggle(id)',group)
                assert original_display==await page.evaluate('JSON.stringify(__canvasTest.display())')
            await page.locator('[data-graph-viewport="fit"]').click()
            await page.locator('[data-component="'+first['focus']+'"]').click()
            await page.locator('[data-graph-focus="isolate"]').click()
            assert await page.locator('svg .is-isolated-out').count()>0
            await page.locator('[data-graph-focus="clear"]').click()
            assert await page.locator('svg .is-isolated-out').count()==0
            await page.locator('[data-canvas-inspector]').click()
            await page.set_viewport_size({'width':760,'height':837})
            await page.locator('[data-graph-viewport="fit"]').click()
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.screenshot(path=str(args.output/(sample['sample_id']+'-compact.png')))
            await page.set_viewport_size({'width':390,'height':844})
            await page.locator('[data-graph-viewport="fit"]').click()
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.screenshot(path=str(args.output/(sample['sample_id']+'-mobile.png')))
            await page.set_viewport_size({'width':1426,'height':837})
            assert before==await page.evaluate('window.__canvasTest.canonical()')
            results.append(dict(sample=sample['sample_id'],**meta,domain_checks=domain_checks,inspection_checks=inspection_checks,summary_checks=summary_checks,selected_edges=selected_edges,reciprocal_bundles=len(bundles),journeys=journeys,arrow_checks=arrow_checks,collapsed_groups=len(group_ids),text_overflows=overflow,canonical_unchanged=True,errors=errors))
            print(json.dumps(results[-1],ensure_ascii=True),flush=True)
            await page.close()
        await browser.close()
    (args.output/'visual-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    assert all(not item['errors'] for item in results), results


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--candidate',type=Path)
    parser.add_argument('--fixtures',type=Path,default=Path(__file__).resolve().parent/'playwright_artifacts/canvas-readability')
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'playwright_artifacts/canvas-readability/views')
    asyncio.run(run(parser.parse_args()))
