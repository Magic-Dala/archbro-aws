"""Export a single-file, offline reference reader using the actual Canvas renderer.

The output has no authentication, API or provider capability. The only source
adaptations disable startup/auth and bind the frozen fixtures to the renderer.
Production files and accepted fixture architecture remain unchanged.
"""
import argparse
import base64
import json
import re
from pathlib import Path


def export(repo, fixtures, output):
    web=repo/'frontend/web'
    samples=[json.loads(path.read_text(encoding='utf-8')) for path in sorted(fixtures.glob('0*-canvas.json'))]
    if len(samples)!=3 or any(not s['canvas'].get('presentation') for s in samples):
        raise ValueError('Expected the three version-pinned English reference envelopes')
    page=(web/'index.html').read_text(encoding='utf-8')
    page=re.sub(r'<script[^>]*>.*?</script>','',page,flags=re.S)
    page=re.sub(r'<link[^>]*rel="stylesheet"[^>]*>','',page)
    page=re.sub(r'<link[^>]*rel="icon"[^>]*>','',page)
    logo='data:image/svg+xml;base64,'+base64.b64encode((web/'archbro-logo.svg').read_bytes()).decode()
    page=re.sub(r'/static/archbro-logo.svg\?v=[^"\s]+',lambda match:logo,page)
    css='\n'.join((web/name).read_text(encoding='utf-8') for name in ['styles.css','progress.css'])
    css+='''
    .reference-switch{display:grid;gap:5px;min-width:0}.reference-switch small{font-size:8px;letter-spacing:1px;color:#839791}
    .reference-switch select{font:600 11px system-ui;color:#315e53;border:1px solid #d4e2dc;background:#fff;padding:9px 28px 9px 10px;border-radius:8px;max-width:260px}
    .reference-note{font-size:9px;color:#7c958b;white-space:nowrap}.reference-note:before{content:'●';color:#8caaa0;margin-right:6px}
    .architecture-canvas-mode #graphCanvas .graph-meta{padding-right:14px}
    @media(max-width:760px){.reference-switch select{max-width:210px}.reference-note{font-size:8px}}
    '''
    page=page.replace('</head>','<style>'+css+'</style></head>')
    script=(web/'app.js').read_text(encoding='utf-8')
    match=re.search(r"import\s*\{(.*?)\}\s*from './firebase-auth\.js[^']*';",script,re.S)
    names=[name.strip() for name in match.group(1).split(',') if name.strip()]
    disabled='\n'.join(f'const {name}=()=>{{throw new Error("Sign-in is unavailable in this offline reference reader.");}};' for name in names)
    script=script[:match.start()]+disabled+script[match.end():]
    script=script.replace("const ARCHITECTURE_CANVAS_MODE = URL_PARAMS.get('canvas') === 'architecture';",'const ARCHITECTURE_CANVAS_MODE = true;')
    script=script.replace('appInitializationPromise = initialize();','/* Offline reference startup below. */')
    bootstrap='''
const referenceSamples=JSON.parse(document.getElementById('referenceData').textContent);
const chosen=referenceSamples.find(item=>item.sample.sample_id===URL_PARAMS.get('sample')) || referenceSamples[0];
api=async()=>{throw new Error('This reference reader is offline. Open the signed-in workspace for API and agent actions.');};
updateInstructionContext=()=>{};
state.projectId=chosen.sample.sample_id;
state.project={id:state.projectId,name:chosen.canvas.presentation.title,goal:chosen.canvas.presentation.subtitle};
state.architecture=chosen.architecture;state.diagram=normalizeFullCanvasResponse(chosen.canvas);
state.tasks=[];state.projects=[state.project];state.currentView='architecture';state.onboarding.active=false;
document.body.classList.add('architecture-canvas-mode');
$('workspaceShell').classList.remove('hidden');$('workspace').classList.remove('hidden');
document.querySelectorAll('.view').forEach(el=>el.classList.toggle('active',el.id==='view-architecture'));
$('entryExperience').classList.add('hidden');$('emptyState').classList.add('hidden');
$('architectureCanvasBtn').outerHTML=`<label class="reference-switch"><small>REFERENCE COLLECTION / ENGLISH</small><select aria-label="Reference project">${referenceSamples.map((item,index)=>`<option value="${item.sample.sample_id}"${item===chosen?' selected':''}>${String(index+1).padStart(2,'0')} · ${escapeHtml(item.canvas.presentation.title)}</option>`).join('')}</select></label>`;
document.querySelector('.reference-switch select').addEventListener('change',event=>{location.search='?sample='+encodeURIComponent(event.target.value);});
document.querySelector('.architecture-mode-switch').hidden=true;
$('graphVersion').textContent='v'+state.architecture.version;
const renderReferenceGraph=renderGraph;
renderGraph=()=>{
  renderReferenceGraph();
  document.querySelector('.architecture-mode-switch').style.display='none';
  document.querySelectorAll('[data-trace-path],[data-agent-explore]').forEach(button=>{button.disabled=true;button.title='Available in the signed-in workspace';});
  const note=document.createElement('span');note.className='reference-note';note.textContent='Offline reference · No live data';
  document.querySelector('.graph-meta')?.append(note);
};
const requestedReferenceNode=new URLSearchParams(location.search).get('node');
renderGraph();
const initialReferenceNode=state.diagram.nodes.find(node=>node.component_id===requestedReferenceNode || node.id===requestedReferenceNode);
if(initialReferenceNode) activateGraphNode(initialReferenceNode);
document.title=chosen.canvas.presentation.title+' — ArchBro Reference Collection';
'''
    data=json.dumps(samples,ensure_ascii=False).replace('<','\\u003c')
    prototype=(web/'prototype.js').read_text(encoding='utf-8')
    scripts='<script id="referenceData" type="application/json">'+data+'</script>'
    scripts+='<script>'+prototype.replace('</script','<\\/script')+'</script>'
    scripts+='<script type="module">'+(script+'\n'+bootstrap).replace('</script','<\\/script')+'</script>'
    page=page.replace('</body>',scripts+'</body>')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(page,encoding='utf-8',newline='\n')
    print(json.dumps({'output':str(output),'bytes':output.stat().st_size,'samples':len(samples),'mode':'OFFLINE_REFERENCE_READER'}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--fixtures',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    export(args.repo,args.fixtures,args.output)
