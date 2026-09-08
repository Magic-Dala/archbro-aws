"""Capture fixed views in the user's existing native Workbench browser.

This connects to the already enabled CDP surface for DOM observation and viewport
buttons only. Business validation stays on WebMCP; no browser/profile is launched.
"""
import argparse
import asyncio
import json
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from reference_projects import CORPUS, REPO, SAMPLES, Workbench, digest, read, require, write


async def capture(args):
    from playwright.async_api import async_playwright
    require(urlsplit(args.cdp).hostname in ('127.0.0.1', 'localhost'), 'Existing CDP must be local')
    registry = read(args.registry)
    wb = Workbench(args.workbench, registry['target_origin'], args.evidence)
    initial = wb.ready()
    failures = []
    records = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(args.cdp)
        # Match the native document identity, not merely another tab with the same URL.
        pages = [page for context in browser.contexts for page in context.pages if page.url.startswith(wb.target + '/')]
        selected = []
        for candidate in pages:
            document_id = await candidate.evaluate('globalThis.__zenuWebMCPHost?.snapshot?.()?.documentId')
            if document_id == initial['documentId']:
                selected.append(candidate)
        require(len(selected) == 1, 'Cannot identify the existing native Workbench document')
        page = selected[0]
        page.on('pageerror', lambda error: failures.append(dict(kind='pageerror', message=str(error))))
        page.on('console', lambda message: failures.append(dict(kind='console', message=message.text)) if message.type == 'error' else None)
        page.on('response', lambda response: failures.append(dict(kind='http', status=response.status,
                url=response.url.split('?')[0])) if response.status >= 400 else None)
        page.on('requestfailed', lambda request: failures.append(dict(kind='network',
                url=request.url.split('?')[0], failure=request.failure)))

        async def save_view(key, mode):
            path = args.evidence / f'{key}-{mode}.png'
            # Native capture is reliable when Workbench is behind another app.
            def native_capture():
                with urllib.request.urlopen(wb.base + '/v1/browser/screenshot?scale=1', timeout=25) as response:
                    return response.read()
            # Flush the native compositor before saving: its first frame can still
            # show the preceding zoom even though DOM state is already updated.
            await page.wait_for_load_state('networkidle', timeout=10000)
            await asyncio.to_thread(native_capture)
            await asyncio.sleep(0.5)
            path.write_bytes(await asyncio.to_thread(native_capture))
            metadata = await page.evaluate('''() => ({
                url:location.href, viewport:{width:innerWidth,height:innerHeight,dpr:devicePixelRatio},
                nodes:document.querySelectorAll('svg.living-graph-svg [data-node]').length,
                visual_connections:document.querySelectorAll('svg.living-graph-svg [data-edge]').length,
                reading_mode:document.querySelector('.graph-stage')?.dataset.readingMode,
                zoom_tier:document.querySelector('#graphCanvas')?.dataset.zoomTier,
                zoom:document.querySelector('[data-graph-zoom]')?.textContent,
                graph_meta:document.querySelector('.graph-meta')?.textContent,
                selected_node:document.querySelector('[data-node].selected')?.dataset.node ?? null,
                view_box:document.querySelector('svg.living-graph-svg')?.getAttribute('viewBox')
            })''')
            record = dict(sample_id=key, view=mode, screenshot=str(path), **metadata)
            write(path.with_suffix('.json'), record)
            records.append(record)

        for key in SAMPLES:
            sample = read(CORPUS / f'{key}.json')
            entry = registry['samples'][key]
            require(entry['status'] == 'VERIFIED', 'Publish/verify the owned reference project first')
            project = entry['project_id']
            base_query = dict(canvas='architecture', project=project, reference='v1')
            wb.request('/v1/browser/navigate', {'url': wb.target + '/?' + urlencode(base_query)})
            wb.ready(project)
            await page.locator('svg.living-graph-svg').wait_for(state='visible', timeout=15000)
            await page.locator('[data-graph-viewport="fit"]').click()
            await save_view(key, 'overview')

            focus = sample['scenarios'][0]['focus']
            wb.request('/v1/browser/navigate', {'url': wb.target + '/?' + urlencode(base_query | dict(node=focus))})
            wb.ready(project)
            await page.locator(f'[data-node="node:{focus}"].selected').wait_for(state='attached', timeout=15000)
            await page.locator('[data-graph-viewport="actual"]').click()
            await page.locator('[data-graph-viewport="zoom-in"]').click()
            await page.wait_for_function("document.querySelector('[data-graph-zoom]')?.textContent === '125%'")
            await save_view(key, 'detail-125')
            after = wb.tool('archbro_get_architecture_decision_context', {}, f'{key}-after-view')
            require(digest(after['architecture']) == entry['architecture_sha256'], 'View interactions changed accepted architecture')
            await page.wait_for_load_state('networkidle', timeout=10000)
            print(json.dumps(dict(sample_id=key, captured_views=2, architecture_unchanged=True)), flush=True)

    write(args.evidence / 'view-baseline.json', dict(schema='archbro.reference-view-baseline.v1',
          views=records, failures=failures, architecture_unchanged=True,
          status='CAPTURED', visual_readability='RECORDED_FOR_COMPARISON'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, default=REPO / '.demo/reference-projects/registry.json')
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--workbench', default='http://127.0.0.1:5180/zenu-app')
    parser.add_argument('--cdp', default='http://127.0.0.1:9352')
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    asyncio.run(capture(args))


if __name__ == '__main__':
    main()
