import {getFirebaseIdToken} from './firebase-auth.js';

const TOOL_PREFIX = 'archbro_';
const WEBMCP_SURFACE_VERSION = 'archbro.semantic-webmcp.v4';
const WEBMCP_MANIFEST_URL = '/webmcp-manifest.json';
const WEBMCP_RUNTIME_CHECK_INTERVAL_MS = 10_000;
const EXTERNAL_RESULT_INLINE_MAX_CHARS = 8_000;
const EXTERNAL_RESULT_RECOVERY_CHARS = 6_000;
const EXTERNAL_RESULT_RECOVERY_MAX_CHARS = 12_000;
const EXTERNAL_RESULT_CACHE_SIZE = 8;
let externalResultSequence = 0;
const externalResultCache = new Map();
let staleReloadScheduled = false;

const WEBMCP_MODEL_TOOL_DESCRIPTIONS = {
  [`${TOOL_PREFIX}ping`]: 'Check WebMCP build.',
  [`${TOOL_PREFIX}get_agent_context`]: 'Read project/node context.',
  [`${TOOL_PREFIX}get_architecture_diagram`]: 'Read architecture layout.',
  [`${TOOL_PREFIX}publish_code_architecture`]: 'Publish revision-pinned code evidence.',
  [`${TOOL_PREFIX}get_code_architecture`]: 'Read implementation evidence.',
  [`${TOOL_PREFIX}get_architecture_node_context`]: 'Read authored dependency context.',
  [`${TOOL_PREFIX}find_architecture_path`]: 'Find an authored architecture path.',
  [`${TOOL_PREFIX}bootstrap_project`]: 'Create Architecture v1; obey SYSTEM_MAP roots and depth-first preorder.',
  [`${TOOL_PREFIX}expand_architecture_scope`]: 'Propose one child level for review.',
  [`${TOOL_PREFIX}get_architecture_decision_context`]: 'Read architecture decision evidence.',
  [`${TOOL_PREFIX}submit_architecture_recommendation`]: 'Submit architecture advice for review.',
  [`${TOOL_PREFIX}create_task`]: 'Create a task without model use.',
  [`${TOOL_PREFIX}update_task_status`]: 'Start or complete a task.',
  [`${TOOL_PREFIX}record_project_observation`]: 'Record evidence without architecture change.',
  [`${TOOL_PREFIX}list_connected_mcp_servers`]: 'List connected MCP sources.',
  [`${TOOL_PREFIX}list_connected_mcp_tools`]: 'List tools from a connected MCP.',
  [`${TOOL_PREFIX}call_connected_mcp_tool`]: 'Call connected MCP; large output uses result_ref.',
};

function compactInputSchemaForModel(value, {propertyMap = false} = {}) {
  if (Array.isArray(value)) return value.map(compactInputSchemaForModel);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => propertyMap || key !== 'description')
      .map(([key, nested]) => [
        key,
        compactInputSchemaForModel(nested, {propertyMap: !propertyMap && key === 'properties'}),
      ]),
  );
}

function compactToolForModel(tool) {
  const inputSchema = compactInputSchemaForModel(tool.inputSchema);
  if (tool.name === `${TOOL_PREFIX}bootstrap_project`) {
    const properties = inputSchema?.properties;
    const planning = properties?.planning_trace?.properties;
    if (properties?.components) {
      properties.components.description = 'SYSTEM_MAP roots; each must have children.';
    }
    if (planning?.system_map_root_ids) {
      planning.system_map_root_ids.description = 'Root ids in component order; all EXPANDED.';
    }
    if (planning?.scope_evaluations) {
      planning.scope_evaluations.description = 'One per component in depth-first preorder.';
    }
    const evaluation = planning?.scope_evaluations?.items?.properties;
    if (evaluation?.child_ids) {
      evaluation.child_ids.description = 'EXPANDED: ordered child ids; JUSTIFIED_LEAF: empty.';
    }
  }
  return {
    ...tool,
    description: WEBMCP_MODEL_TOOL_DESCRIPTIONS[tool.name] || tool.description,
    inputSchema,
  };
}

async function fetchWebMcpManifest({signal} = {}) {
  const response = await fetch(WEBMCP_MANIFEST_URL, {
    method: 'GET',
    signal,
    cache: 'no-store',
    headers: {'Accept': 'application/json'},
  });
  if (!response.ok) throw new Error(`${response.status}: ArchBro WebMCP manifest request failed`);
  return response.json();
}

async function verifyWebMcpRuntime({signal, autoReload = false} = {}) {
  const manifest = await fetchWebMcpManifest({signal});
  const loadedAssetSha256 = globalThis.window?.__ARCHBRO_RUNTIME_CONFIG__?.webmcp_asset_sha256 || null;
  const surfaceVersionMatch = manifest.surface_version === WEBMCP_SURFACE_VERSION;
  const assetMatch = !loadedAssetSha256 || manifest.asset_sha256 === loadedAssetSha256;
  const staleClient = !surfaceVersionMatch || !assetMatch;

  if (staleClient && autoReload && !staleReloadScheduled && globalThis.window?.location) {
    staleReloadScheduled = true;
    const nextUrl = new URL(globalThis.window.location.href);
    nextUrl.searchParams.set('_archbro_webmcp_build', String(manifest.asset_sha256 || Date.now()).slice(0, 16));
    globalThis.window.location.replace(nextUrl.toString());
  }

  return {
    manifest,
    stale_client: staleClient,
    reload_required: staleClient,
    client_surface_version: WEBMCP_SURFACE_VERSION,
    server_surface_version: manifest.surface_version,
    asset_match: assetMatch,
  };
}

function requireBridgeMethod(bridge, method) {
  if (!bridge || typeof bridge[method] !== 'function') {
    throw new Error(`ArchBroWebBridge.${method}() is required`);
  }
}

function hasBridgeMethod(bridge, method) {
  return Boolean(bridge && typeof bridge[method] === 'function');
}

function asToolResult(value) {
  if (typeof value === 'string') return value;
  return JSON.stringify(value ?? null);
}

function externalResultCap(key = '') {
  const normalized = String(key).toLowerCase();
  if (normalized.includes('error') || normalized.includes('exception')) return 20;
  if (normalized.includes('warning') || normalized.includes('failure') || normalized.includes('xfail')) return 10;
  if (normalized.includes('inventory') || normalized.includes('package') || normalized.includes('dependency') || normalized.includes('image')) return 50;
  return 20;
}

function compactExternalValue(value, key = 'result', depth = 0) {
  if (typeof value === 'string') {
    if (value.length <= 1_600) return value;
    const head = value.slice(0, 1_200);
    const tail = value.slice(-240);
    return {
      excerpt: `${head}\n… ${value.length - head.length - tail.length} chars omitted …\n${tail}`,
      original_chars: value.length,
      truncated: true,
    };
  }
  if (Array.isArray(value)) {
    const cap = externalResultCap(key);
    const items = value.slice(0, cap).map((item) => compactExternalValue(item, key, depth + 1));
    if (value.length <= cap) return items;
    return {items, shown: items.length, total: value.length, overflow_count: value.length - items.length, truncated: true};
  }
  if (!value || typeof value !== 'object') return value;
  if (depth >= 6) return {summary: '[nested result omitted]', truncated: true};
  return Object.fromEntries(
    Object.entries(value).map(([nestedKey, nestedValue]) => [nestedKey, compactExternalValue(nestedValue, nestedKey, depth + 1)]),
  );
}

function rememberExternalResult(raw, {serverId, toolName}) {
  const ref = `webmcp-result:${Date.now().toString(36)}:${++externalResultSequence}`;
  externalResultCache.set(ref, {raw, serverId, toolName, binding: activeProjectBinding()});
  while (externalResultCache.size > EXTERNAL_RESULT_CACHE_SIZE) {
    externalResultCache.delete(externalResultCache.keys().next().value);
  }
  return ref;
}

function readCachedExternalResult({resultRef, serverId, toolName, offset = 0, maxChars = EXTERNAL_RESULT_RECOVERY_CHARS}) {
  const cached = externalResultCache.get(resultRef);
  if (!cached || cached.serverId !== serverId || cached.toolName !== toolName) {
    throw new Error('Connected MCP result_ref is missing, expired, or belongs to a different tool.');
  }
  const currentBinding = activeProjectBinding();
  if (!cached.binding || cached.binding.projectId !== currentBinding.projectId) {
    throw new Error('Connected MCP result_ref belongs to a different project.');
  }
  assertActiveProjectBinding(cached.binding);
  const start = Math.max(0, Number.isFinite(Number(offset)) ? Math.trunc(Number(offset)) : 0);
  const requested = Number.isFinite(Number(maxChars)) ? Math.trunc(Number(maxChars)) : EXTERNAL_RESULT_RECOVERY_CHARS;
  const size = Math.max(1, Math.min(EXTERNAL_RESULT_RECOVERY_MAX_CHARS, requested));
  const content = cached.raw.slice(start, start + size);
  const nextOffset = start + content.length;
  return {
    schema: 'archbro.bounded_result_slice.v1',
    full_result_ref: resultRef,
    server_id: serverId,
    tool_name: toolName,
    offset: start,
    next_offset: nextOffset,
    complete: nextOffset >= cached.raw.length,
    total_chars: cached.raw.length,
    content,
  };
}

function boundedConnectedMcpResult(payload, {serverId, toolName}) {
  const raw = typeof payload === 'string' ? payload : JSON.stringify(payload ?? null);
  if (raw.length <= EXTERNAL_RESULT_INLINE_MAX_CHARS) return payload;

  const fullResultRef = rememberExternalResult(raw, {serverId, toolName});
  let compacted = compactExternalValue(payload);
  let compactedJson = typeof compacted === 'string' ? compacted : JSON.stringify(compacted);
  if (compactedJson.length > EXTERNAL_RESULT_INLINE_MAX_CHARS - 1_000) {
    const head = compactedJson.slice(0, EXTERNAL_RESULT_INLINE_MAX_CHARS - 2_000);
    const tail = compactedJson.slice(-600);
    compacted = {
      excerpt: `${head}\n… ${compactedJson.length - head.length - tail.length} compacted chars omitted …\n${tail}`,
      truncated: true,
    };
    compactedJson = JSON.stringify(compacted);
  }
  return {
    schema: 'archbro.bounded_result.v1',
    classification: 'EXTERNAL_EVIDENCE',
    canonical_state_mutated: false,
    summary: `Connected MCP result bounded from ${raw.length} chars to ${compactedJson.length} chars.`,
    result: compacted,
    truncated: true,
    original_chars: raw.length,
    full_result_ref: fullResultRef,
    recovery: {server_id: serverId, tool_name: toolName, offset: 0, max_chars: EXTERNAL_RESULT_RECOVERY_CHARS},
  };
}

function compactCodeArchitectureForTool(payload) {
  if (!payload || typeof payload !== 'object') return payload;
  const compactSource = (source) => {
    if (!source || typeof source !== 'object') return source;
    const {excerpt: _excerpt, ...reference} = source;
    return reference;
  };
  const diagram = payload.diagram && typeof payload.diagram === 'object'
    ? {
        ...payload.diagram,
        nodes: Array.isArray(payload.diagram.nodes)
          ? payload.diagram.nodes.map((node) => ({
              ...node,
              sources: Array.isArray(node.sources) ? node.sources.map(compactSource) : node.sources,
            }))
          : payload.diagram.nodes,
        edges: Array.isArray(payload.diagram.edges)
          ? payload.diagram.edges.map((edge) => ({
              ...edge,
              sources: Array.isArray(edge.sources) ? edge.sources.map(compactSource) : edge.sources,
            }))
          : payload.diagram.edges,
      }
    : payload.diagram;
  return {...payload, diagram};
}

function activeProjectBinding() {
  const bridge = globalThis.window?.ArchBroWebBridge;
  const committedNavigation = bridge?.getCommittedNavigation?.();
  if (typeof bridge?.getCommittedNavigation === 'function') {
    if (!committedNavigation?.initialized) {
      throw new Error('ArchBro navigation is still initializing.');
    }
    const committedProjectId = String(committedNavigation.project_id || '').trim();
    if (!committedProjectId) throw new Error('No active ArchBro project is selected.');
    const bridgeBinding = bridge.getActiveProjectBinding?.();
    if (
      !bridgeBinding
      || String(bridgeBinding.projectId || '').trim() !== committedProjectId
    ) {
      throw new Error('ArchBro project navigation changed before the tool request could bind to it. Retry against the project currently on screen.');
    }
    return {
      projectId: committedProjectId,
      generation: Number.isInteger(bridgeBinding.generation) ? bridgeBinding.generation : null,
      repositoryRevision: Number.isInteger(bridgeBinding.repositoryRevision) ? bridgeBinding.repositoryRevision : null,
      source: 'bridge',
    };
  }

  // Legacy embedding surfaces without the transactional navigation bridge keep
  // the older discovery path. Once committed navigation exists, URL/storage are
  // never allowed to widen or resurrect a stale project.
  const bridgeBinding = bridge?.getActiveProjectBinding?.();
  if (bridgeBinding && typeof bridgeBinding.projectId === 'string' && bridgeBinding.projectId.trim()) {
    return {
      projectId: bridgeBinding.projectId.trim(),
      generation: Number.isInteger(bridgeBinding.generation) ? bridgeBinding.generation : null,
      repositoryRevision: Number.isInteger(bridgeBinding.repositoryRevision) ? bridgeBinding.repositoryRevision : null,
      source: 'bridge',
    };
  }
  const bridgeProjectId = bridge?.getActiveProjectId?.();
  if (typeof bridgeProjectId === 'string' && bridgeProjectId.trim()) {
    return {projectId: bridgeProjectId.trim(), generation: null, source: 'bridge'};
  }
  const requestedProjectId = new URLSearchParams(globalThis.location?.search || '').get('project')?.trim();
  if (requestedProjectId) return {projectId: requestedProjectId, generation: null, source: 'url'};
  const storedProjectId = globalThis.localStorage?.getItem('archbro-project-id')?.trim();
  if (storedProjectId) return {projectId: storedProjectId, generation: null, source: 'storage'};
  throw new Error('No active ArchBro project is selected.');
}

function activeProjectId() {
  return activeProjectBinding().projectId;
}

function assertActiveProjectBinding(binding) {
  if (!binding || binding.source !== 'bridge') return;
  const bridge = globalThis.window?.ArchBroWebBridge;
  if (typeof bridge?.getCommittedNavigation === 'function') {
    const committed = bridge.getCommittedNavigation();
    if (!committed?.initialized || committed.project_id !== binding.projectId) {
      throw new Error('Active ArchBro project changed while the tool request was being prepared. Retry against the project currently on screen.');
    }
  }
  if (binding.generation === null) return;
  const current = bridge?.getActiveProjectBinding?.();
  if (
    !current
    || current.projectId !== binding.projectId
    || current.generation !== binding.generation
    || (binding.repositoryRevision != null && current.repositoryRevision !== binding.repositoryRevision)
  ) {
    throw new Error('Active ArchBro project changed while the tool request was being prepared. Retry against the project currently on screen.');
  }
}

async function agentSurfaceApi(path, {method = 'GET', body, signal, projectBinding} = {}) {
  const token = await getFirebaseIdToken();
  if (projectBinding) assertActiveProjectBinding(projectBinding);
  const response = await fetch(path, {
    method,
    signal,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? {Authorization: `Bearer ${token}`} : {}),
    },
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  if (projectBinding) assertActiveProjectBinding(projectBinding);
  if (!response.ok) {
    let detail = 'ArchBro agent surface request failed';
    try {
      const payload = await response.json();
      const rawDetail = payload.detail ?? payload;
      detail = typeof rawDetail === 'string' ? rawDetail : JSON.stringify(rawDetail);
    } catch {}
    throw new Error(`${response.status}: ${detail}`);
  }
  return response.status === 204 ? null : response.json();
}

function providerConnectionIsDiscoverable(connection) {
  if (!connection?.id || connection.authorization_pending) return false;
  if (connection.last_probe_ok === true) return true;
  return (
    connection.last_probe_ok !== false
    && !connection.last_error
    && connection.restored === true
    && connection.persistent === true
    && connection.has_credentials === true
    && ['oauth', 'microsoft_teams_oauth'].includes(connection.auth_type)
  );
}

async function listAuthorizedProviderConnections({signal} = {}) {
  const connections = await agentSurfaceApi('/mcp/connections', {signal});
  return (connections || []).filter(providerConnectionIsDiscoverable);
}

function providerConnectionAsServer(connection) {
  return {
    id: connection.id,
    name: connection.name || connection.provider || 'Connected MCP',
    description: connection.endpoint || `${connection.provider || 'provider'} connection authorized by the current user`,
    provider: connection.provider || null,
    source_kind: 'authorized_provider',
    tool_count: connection.tool_count ?? null,
    auth_configured: Boolean(connection.has_credentials),
  };
}

async function providerConnectionById(serverId, {signal} = {}) {
  const connections = await listAuthorizedProviderConnections({signal});
  return connections.find((connection) => connection.id === serverId) || null;
}

function mergeProviderConnectionsIntoAgentContext(context, connections) {
  if (!context || !connections.length) return context;
  const providerLines = connections.map((connection) => (
    `- ${connection.id}: ${connection.name || connection.provider || 'Connected MCP'}`
    + ` — ${connection.endpoint || 'authorized provider connection'}`
    + (connection.tool_count === null || connection.tool_count === undefined ? '' : ` (${connection.tool_count} tools)`)
  ));
  let content = String(context.content || '');
  const emptyExternalSources = '## External Sources\n- none configured';
  if (content.includes(emptyExternalSources)) {
    content = content.replace(emptyExternalSources, `## External Sources\n${providerLines.join('\n')}`);
  } else {
    const routingMarker = '\n## Routing';
    const insertion = `\n${providerLines.join('\n')}`;
    content = content.includes(routingMarker)
      ? content.replace(routingMarker, `${insertion}${routingMarker}`)
      : `${content}${insertion}`;
  }
  return {
    ...context,
    connected_source_count: Number(context.connected_source_count || 0) + connections.length,
    content,
  };
}

function agentContextExecutionRequest(manifest) {
  const selection = manifest?.selection || {};
  if (
    manifest?.schema !== 'archbro.agent_context_manifest.v1'
    || !selection.node_id
    || !selection.direction
    || !selection.expansion_policy
    || !Number.isInteger(manifest.architecture_version)
    || !/^[0-9a-f]{64}$/i.test(String(manifest.manifest_hash || ''))
  ) {
    throw new Error('Server returned an invalid bounded Agent Context Manifest.');
  }
  return {
    node_id: selection.node_id,
    direction: selection.direction,
    expansion_policy: selection.expansion_policy,
    expected_architecture_version: manifest.architecture_version,
    preview_manifest_hash: manifest.manifest_hash,
  };
}

async function getAgentContext({nodeId, expectedArchitectureVersion, signal} = {}) {
  const projectBinding = activeProjectBinding();
  const projectId = projectBinding.projectId;
  if (nodeId) {
    if (!Number.isInteger(expectedArchitectureVersion) || expectedArchitectureVersion < 1) {
      throw new Error('expected_architecture_version >= 1 is required for bounded Agent Context preview.');
    }
    const manifest = await agentSurfaceApi(`/projects/${encodeURIComponent(projectId)}/agent-context/manifest`, {
      method: 'POST',
      body: {
        node_id: nodeId,
        direction: 'both',
        expansion_policy: 'ASK_ALL',
        expected_architecture_version: expectedArchitectureVersion,
      },
      signal,
      projectBinding,
    });
    return {
      mode: 'BOUNDED_NODE',
      manifest,
      agent_context_request: agentContextExecutionRequest(manifest),
    };
  }
  if (expectedArchitectureVersion !== undefined && expectedArchitectureVersion !== null) {
    throw new Error('expected_architecture_version is only valid when node_id is provided.');
  }
  const context = await agentSurfaceApi(`/projects/${encodeURIComponent(projectId)}/agent-context`, {signal, projectBinding});
  let providerConnections = [];
  try {
    providerConnections = await listAuthorizedProviderConnections({signal});
  } catch {
    // Keep canonical project context readable if the optional provider hub is unavailable.
  }
  return mergeProviderConnectionsIntoAgentContext(context, providerConnections);
}

function querySuffix(entries) {
  const query = new URLSearchParams();
  for (const [key, value] of entries) {
    if (value !== undefined && value !== null) query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : '';
}

const ARCHITECTURE_KIND_VALUES = [
  'SYSTEM', 'UI', 'SERVICE', 'AGENT', 'TOOL', 'DATA_STORE', 'STATE',
  'EXTERNAL_SERVICE', 'INFRASTRUCTURE',
];

function architectureComponentInputSchema(depth = 1) {
  const properties = {
    id: {type: 'string', minLength: 1, description: 'Optional stable component id. Strongly recommended when relationships or tasks reference this component.'},
    name: {type: 'string', minLength: 1},
    type: {type: 'string', minLength: 1},
    responsibility: {type: 'string', minLength: 1},
    kind: {type: 'string', enum: ARCHITECTURE_KIND_VALUES},
    status: {type: 'string', minLength: 1},
  };
  if (depth < 3) {
    properties.children = {
      type: 'array',
      maxItems: depth === 1 ? 7 : 6,
      description: `Immediate canonical children at hierarchy level ${depth + 1}.`,
      items: architectureComponentInputSchema(depth + 1),
    };
  }
  return {
    type: 'object',
    properties,
    required: ['id', 'name', 'type', 'responsibility'],
    additionalProperties: false,
  };
}

function initialPlanningTraceInputSchema() {
  return {
    type: 'object',
    description: 'Auditable recursive outside-in planning trace. Plan SYSTEM_MAP roots, then evaluate every canonical component in preorder. A scope is EXPANDED only when it has real child architecture boundaries; otherwise it must be a JUSTIFIED_LEAF with a specific reason. Reconcile relationships/tasks only after every scope is evaluated.',
    properties: {
      system_map_root_ids: {
        type: 'array', minItems: 1, maxItems: 6,
        items: {type: 'string', minLength: 1},
        description: 'Stable root ids from the SYSTEM_MAP phase, in final root order. Every SYSTEM_MAP root is a broad architecture boundary and must be EXPANDED with at least one child; atomic services/components belong below a root.',
      },
      scope_evaluations: {
        type: 'array', minItems: 1, maxItems: 80,
        description: 'Exactly one evaluation for every submitted canonical component in preorder. Do not mark a multi-responsibility boundary as a leaf merely to avoid decomposition.',
        items: {
          type: 'object',
          properties: {
            scope_component_id: {type: 'string', minLength: 1},
            decomposition: {type: 'string', enum: ['EXPANDED', 'JUSTIFIED_LEAF']},
            child_ids: {type: 'array', maxItems: 12, items: {type: 'string', minLength: 1}, description: 'Immediate child ids in final order. Non-empty for EXPANDED; empty for JUSTIFIED_LEAF.'},
            leaf_reason: {type: 'string', minLength: 24, maxLength: 280, description: 'Required only for JUSTIFIED_LEAF. Explain why no independently addressable architecture boundary remains below this component.'},
          },
          required: ['scope_component_id', 'decomposition', 'child_ids'],
          additionalProperties: false,
        },
      },
      reconciled: {
        type: 'boolean',
        enum: [true],
        description: 'Must be true only after every scope is evaluated and final authored relationships/tasks are reconciled against the complete topology.',
      },
    },
    required: ['system_map_root_ids', 'scope_evaluations', 'reconciled'],
    additionalProperties: false,
  };
}

function codeArchitectureComponentInputSchema(depth = 1) {
  const properties = {
    id: {type: 'string', minLength: 1, description: 'Snapshot-local stable implementation component id.'},
    name: {type: 'string', minLength: 1},
    type: {type: 'string', minLength: 1},
    responsibility: {type: 'string', minLength: 1},
    kind: {type: 'string', enum: ARCHITECTURE_KIND_VALUES},
    source_evidence_ids: {
      type: 'array', minItems: 1, maxItems: 12,
      items: {type: 'string', minLength: 1},
      description: 'Evidence ids proving this implementation boundary at the pinned revision.',
    },
  };
  if (depth < 3) {
    properties.children = {
      type: 'array',
      maxItems: depth === 1 ? 7 : 6,
      items: codeArchitectureComponentInputSchema(depth + 1),
    };
  }
  return {
    type: 'object',
    properties,
    required: ['id', 'name', 'type', 'responsibility', 'source_evidence_ids'],
    additionalProperties: false,
  };
}

function codeArchitectureSnapshotInputSchema() {
  return {
    type: 'object',
    properties: {
      repository: {type: 'string', minLength: 3, description: 'GitHub owner/repo or https://github.com/owner/repo.'},
      revision: {type: 'string', pattern: '^[0-9a-fA-F]{40}$', description: 'Exact full Git commit SHA used for every cited source.'},
      summary: {type: 'string', minLength: 1, description: 'Evidence-grounded summary of the implementation architecture.'},
      components: {
        type: 'array', minItems: 1, maxItems: 8,
        description: 'Outside-in implementation hierarchy, up to three levels and 40 total nodes.',
        items: codeArchitectureComponentInputSchema(1),
      },
      relationships: {
        type: 'array', maxItems: 120,
        items: {
          type: 'object',
          properties: {
            source: {type: 'string', minLength: 1},
            target: {type: 'string', minLength: 1},
            relationship_type: {type: 'string', minLength: 1},
            description: {type: 'string'},
            source_evidence_ids: {type: 'array', minItems: 1, maxItems: 12, items: {type: 'string', minLength: 1}},
          },
          required: ['source', 'target', 'relationship_type', 'source_evidence_ids'],
          additionalProperties: false,
        },
      },
      source_evidence: {
        type: 'array', minItems: 1, maxItems: 160,
        description: 'Exact revision-pinned excerpts obtained from the connected GitHub MCP. Excerpt line count must match line_start..line_end.',
        items: {
          type: 'object',
          properties: {
            id: {type: 'string', minLength: 1},
            path: {type: 'string', minLength: 1, description: 'Safe repository-relative POSIX path.'},
            line_start: {type: 'integer', minimum: 1},
            line_end: {type: 'integer', minimum: 1},
            excerpt: {type: 'string', minLength: 1, maxLength: 4000},
            symbol: {type: 'string'},
            blob_sha: {type: 'string', pattern: '^[0-9a-fA-F]{40,64}$'},
          },
          required: ['id', 'path', 'line_start', 'line_end', 'excerpt'],
          additionalProperties: false,
        },
      },
    },
    required: ['repository', 'revision', 'summary', 'components', 'source_evidence'],
    additionalProperties: false,
  };
}

function expansionChildInputSchema() {
  return {
    type: 'object',
    properties: {
      id: {type: 'string', minLength: 1, description: 'New globally stable canonical component id.'},
      name: {type: 'string', minLength: 1},
      type: {type: 'string', minLength: 1},
      responsibility: {type: 'string', minLength: 1},
      kind: {type: 'string', enum: ARCHITECTURE_KIND_VALUES},
      status: {type: 'string', minLength: 1},
    },
    required: ['id', 'name', 'type', 'responsibility'],
    additionalProperties: false,
  };
}

async function getScopedDiagram({scopeComponentId, expectedArchitectureVersion, signal} = {}) {
  const projectBinding = activeProjectBinding();
  const projectId = projectBinding.projectId;
  const suffix = querySuffix([
    ['scope', scopeComponentId],
    ['expected_architecture_version', expectedArchitectureVersion],
  ]);
  return agentSurfaceApi(`/projects/${encodeURIComponent(projectId)}/architecture/diagram${suffix}`, {signal, projectBinding});
}

async function getLatestCodeArchitectureSnapshot({signal} = {}) {
  const projectBinding = activeProjectBinding();
  const projectId = projectBinding.projectId;
  const payload = await agentSurfaceApi(`/projects/${encodeURIComponent(projectId)}/code-architecture/latest`, {signal, projectBinding});
  return compactCodeArchitectureForTool(payload);
}

async function getNodeContext({nodeId, direction, maxHops, maxResults, expectedArchitectureVersion, signal} = {}) {
  const projectBinding = activeProjectBinding();
  const projectId = projectBinding.projectId;
  const suffix = querySuffix([
    ['direction', direction],
    ['max_hops', maxHops],
    ['max_results', maxResults],
    ['expected_architecture_version', expectedArchitectureVersion],
  ]);
  return agentSurfaceApi(
    `/projects/${encodeURIComponent(projectId)}/architecture/nodes/${encodeURIComponent(nodeId)}/context${suffix}`,
    {signal, projectBinding},
  );
}

async function findArchitecturePath({sourceId, targetId, maxHops, expectedArchitectureVersion, signal} = {}) {
  const projectBinding = activeProjectBinding();
  const projectId = projectBinding.projectId;
  const suffix = querySuffix([
    ['source_id', sourceId],
    ['target_id', targetId],
    ['max_hops', maxHops],
    ['expected_architecture_version', expectedArchitectureVersion],
  ]);
  return agentSurfaceApi(`/projects/${encodeURIComponent(projectId)}/architecture/path${suffix}`, {signal, projectBinding});
}

async function listConnectedMcpServers(bridge, {signal} = {}) {
  if (hasBridgeMethod(bridge, 'listConnectedMcpServers')) {
    return bridge.listConnectedMcpServers({signal});
  }
  const projectBinding = activeProjectBinding();
  const projectId = projectBinding.projectId;
  const [projectSources, providerConnections] = await Promise.all([
    agentSurfaceApi(`/projects/${encodeURIComponent(projectId)}/mcp/servers`, {signal, projectBinding}),
    listAuthorizedProviderConnections({signal}),
  ]);
  assertActiveProjectBinding(projectBinding);
  const servers = [...(projectSources?.servers || [])];
  const ids = new Set(servers.map((server) => server.id));
  for (const connection of providerConnections) {
    if (!ids.has(connection.id)) servers.push(providerConnectionAsServer(connection));
  }
  return {project_id: projectId, servers};
}

async function listConnectedMcpTools(bridge, {serverId, signal} = {}) {
  if (hasBridgeMethod(bridge, 'listConnectedMcpTools')) {
    return bridge.listConnectedMcpTools({serverId, signal});
  }
  const projectBinding = activeProjectBinding();
  const projectId = projectBinding.projectId;
  const providerConnection = await providerConnectionById(serverId, {signal});
  assertActiveProjectBinding(projectBinding);
  if (providerConnection) {
    const toolsPath = providerConnection.provider === 'github'
      ? `/projects/${encodeURIComponent(projectId)}/github/tools`
      : `/mcp/connections/${encodeURIComponent(serverId)}/tools`;
    const result = await agentSurfaceApi(toolsPath, {signal, projectBinding});
    return {
      project_id: projectId,
      server_id: serverId,
      source_kind: 'authorized_provider',
      tools: result?.tools || [],
      tool_count: result?.tool_count ?? (result?.tools || []).length,
    };
  }
  return agentSurfaceApi(
    `/projects/${encodeURIComponent(projectId)}/mcp/servers/${encodeURIComponent(serverId)}/tools`,
    {signal, projectBinding},
  );
}

async function callConnectedMcpTool(bridge, {serverId, toolName, arguments: args = {}, resultRef, offset = 0, maxChars = EXTERNAL_RESULT_RECOVERY_CHARS, signal} = {}) {
  if (resultRef) {
    const cached = externalResultCache.get(resultRef);
    if (cached?.binding?.repositoryRevision != null) {
      const current = await agentSurfaceApi(`/projects/${encodeURIComponent(cached.binding.projectId)}/repository`, {signal, projectBinding: cached.binding});
      if (current.repository_revision !== cached.binding.repositoryRevision) throw new Error('Repository selection changed; cached evidence is no longer current.');
    }
    return readCachedExternalResult({resultRef, serverId, toolName, offset, maxChars});
  }

  if (hasBridgeMethod(bridge, 'callConnectedMcpTool')) {
    const binding = activeProjectBinding();
    const result = await bridge.callConnectedMcpTool({serverId, toolName, arguments: args, signal});
    assertActiveProjectBinding(binding);
    return boundedConnectedMcpResult(result, {serverId, toolName});
  }
  const projectBinding = activeProjectBinding();
  const projectId = projectBinding.projectId;
  const providerConnection = await providerConnectionById(serverId, {signal});
  assertActiveProjectBinding(projectBinding);
  if (providerConnection) {
    const isGithub = providerConnection.provider === 'github';
    const callPath = isGithub
      ? `/projects/${encodeURIComponent(projectId)}/github/tools/${encodeURIComponent(toolName)}`
      : `/mcp/connections/${encodeURIComponent(serverId)}/tools/${encodeURIComponent(toolName)}`;
    const body = {arguments: args};
    if (isGithub) {
      body.connection_id = serverId;
      if (projectBinding.repositoryRevision != null) body.expected_repository_revision = projectBinding.repositoryRevision;
    }
    const result = await agentSurfaceApi(callPath, {method: 'POST', body, signal, projectBinding});
    return boundedConnectedMcpResult({
      project_id: projectId,
      server_id: serverId,
      tool_name: toolName,
      result,
      classification: 'EXTERNAL_EVIDENCE',
      canonical_state_mutated: false,
    }, {serverId, toolName});
  }
  const result = await agentSurfaceApi(
    `/projects/${encodeURIComponent(projectId)}/mcp/servers/${encodeURIComponent(serverId)}/call`,
    {method: 'POST', body: {tool_name: toolName, arguments: args}, signal, projectBinding},
  );
  return boundedConnectedMcpResult(result, {serverId, toolName});
}

function createCoreTools(bridge) {
  requireBridgeMethod(bridge, 'bootstrapProject');
  requireBridgeMethod(bridge, 'expandArchitectureScope');
  requireBridgeMethod(bridge, 'getDecisionContext');
  requireBridgeMethod(bridge, 'submitAgentRecommendation');
  requireBridgeMethod(bridge, 'createTask');
  requireBridgeMethod(bridge, 'updateTaskStatus');
  requireBridgeMethod(bridge, 'recordProjectObservation');
  requireBridgeMethod(bridge, 'publishCodeArchitectureSnapshot');

  const tools = [
    {
      name: `${TOOL_PREFIX}ping`,
      title: 'Ping ArchBro Site Tool',
      description: 'Read-only WebMCP capability and build-identity check. Verifies this loaded page is attached to the current server WebMCP manifest without project mutation or model invocation.',
      inputSchema: {type: 'object', properties: {}, additionalProperties: false},
      annotations: {readOnlyHint: true, untrustedContentHint: false},
      execute: async (_input, client = {}) => {
        const runtime = await verifyWebMcpRuntime({signal: client.signal});
        return asToolResult({
          ok: !runtime.stale_client,
          surface: 'archbro-webmcp',
          surface_version: runtime.server_surface_version,
          expected_tool_count: runtime.manifest.expected_tool_count,
          connected_mcp_gateway_configured: runtime.manifest.connected_mcp_gateway_configured,
          stale_client: runtime.stale_client,
          reload_required: runtime.reload_required,
          asset_match: runtime.asset_match,
          built_in_model_called: false,
        });
      },
    },
    {
      name: `${TOOL_PREFIX}get_agent_context`,
      title: 'Get compact ArchBro agent context',
      description: 'Without node_id, bootstrap with the existing compact project map. With node_id and expected_architecture_version, preview the server-owned bounded Agent Context Manifest and return the exact agent_context_request (including preview_manifest_hash) required to execute against the same context. The client never rebuilds or traverses context itself.',
      inputSchema: {
        type: 'object',
        properties: {
          node_id: {type: 'string', pattern: '^node:.+', description: 'Stable canonical node ID for bounded context. Omit for the existing project-level compact context.'},
          expected_architecture_version: {type: 'integer', minimum: 1, description: 'Required with node_id and must be omitted without node_id; the server fails stale versions closed.'},
        },
        additionalProperties: false,
      },
      annotations: {readOnlyHint: true, untrustedContentHint: true},
      execute: async ({node_id, expected_architecture_version}, client = {}) => asToolResult(await getAgentContext({
        nodeId: node_id,
        expectedArchitectureVersion: expected_architecture_version,
        signal: client.signal,
      })),
    },
    {
      name: `${TOOL_PREFIX}get_architecture_diagram`,
      title: 'Get Living Architecture diagram',
      description: 'Read the backend-authored root or one canonical subsystem projection, including SCOPE/PRIMARY/CONTEXT nodes, aggregate-edge provenance, scope metadata, and deterministic positioned graph. Use this to drill architecture one level at a time instead of inferring hierarchy in the host.',
      inputSchema: {
        type: 'object',
        properties: {
          scope_component_id: {type: 'string', minLength: 1, description: 'Plain canonical component id. Omit for the root system map.'},
          expected_architecture_version: {type: 'integer', minimum: 0},
        },
        additionalProperties: false,
      },
      annotations: {readOnlyHint: true, untrustedContentHint: false},
      execute: async ({scope_component_id, expected_architecture_version}, client = {}) => asToolResult(
        await getScopedDiagram({scopeComponentId: scope_component_id, expectedArchitectureVersion: expected_architecture_version, signal: client.signal}),
      ),
    },
    {
      name: `${TOOL_PREFIX}publish_code_architecture`,
      title: 'Publish Code Architecture evidence for this project',
      description: 'Persist an exact-commit implementation architecture snapshot after inspecting the connected GitHub repository. This stores derived implementation evidence for the project UI and later agents; it does not mutate the accepted Living Architecture. Use a full 40-character Git commit SHA and evidence actually read through the connected GitHub MCP.',
      inputSchema: codeArchitectureSnapshotInputSchema(),
      annotations: {readOnlyHint: false, untrustedContentHint: true},
      execute: async ({repository, revision, summary, components, relationships = [], source_evidence}, client = {}) => asToolResult(
        compactCodeArchitectureForTool(await bridge.publishCodeArchitectureSnapshot({repository, revision, summary, components, relationships, sourceEvidence: source_evidence, signal: client.signal})),
      ),
    },
    {
      name: `${TOOL_PREFIX}get_code_architecture`,
      title: 'Get latest published Code Architecture evidence',
      description: 'Read the latest durable implementation architecture snapshot for this project, including exact GitHub revision, source provenance, deterministic layout, and evidence classification. This is derived implementation evidence, not the accepted Living Architecture.',
      inputSchema: {type: 'object', properties: {}, additionalProperties: false},
      annotations: {readOnlyHint: true, untrustedContentHint: true},
      execute: async (_input, client = {}) => asToolResult(await getLatestCodeArchitectureSnapshot({signal: client.signal})),
    },
    {
      name: `${TOOL_PREFIX}get_architecture_node_context`,
      title: 'Get Living Architecture dependency context',
      description: 'Read bounded upstream/downstream authored architecture reachability for one stable node. This is dependency context, not runtime impact or blast-radius analysis.',
      inputSchema: {
        type: 'object',
        properties: {
          node_id: {type: 'string', pattern: '^node:.+', description: 'Stable public ArchBro node ID.'},
          direction: {type: 'string', enum: ['upstream', 'downstream', 'both']},
          max_hops: {type: 'integer', minimum: 1, maximum: 8},
          max_results: {type: 'integer', minimum: 1, maximum: 40},
          expected_architecture_version: {type: 'integer', minimum: 0},
        },
        required: ['node_id'],
        additionalProperties: false,
      },
      annotations: {readOnlyHint: true, untrustedContentHint: false},
      execute: async ({node_id, direction, max_hops, max_results, expected_architecture_version}, client = {}) => asToolResult(
        await getNodeContext({nodeId: node_id, direction, maxHops: max_hops, maxResults: max_results, expectedArchitectureVersion: expected_architecture_version, signal: client.signal}),
      ),
    },
    {
      name: `${TOOL_PREFIX}find_architecture_path`,
      title: 'Find directed authored architecture path',
      description: 'Call the canonical backend Architecture path query and return its authored FOUND, UNREACHABLE, or LIMIT_REACHED result unchanged. A stale expected_architecture_version remains a server 409; no client path traversal is substituted.',
      inputSchema: {
        type: 'object',
        properties: {
          source_id: {type: 'string', pattern: '^node:.+', description: 'Stable source node ID.'},
          target_id: {type: 'string', pattern: '^node:.+', description: 'Stable target node ID.'},
          max_hops: {type: 'integer', minimum: 0, maximum: 8},
          expected_architecture_version: {type: 'integer', minimum: 0},
        },
        required: ['source_id', 'target_id'],
        additionalProperties: false,
      },
      annotations: {readOnlyHint: true, untrustedContentHint: false},
      execute: async ({source_id, target_id, max_hops, expected_architecture_version}, client = {}) => asToolResult(
        await findArchitecturePath({sourceId: source_id, targetId: target_id, maxHops: max_hops, expectedArchitectureVersion: expected_architecture_version, signal: client.signal}),
      ),
    },
    {
      name: `${TOOL_PREFIX}bootstrap_project`,
      title: 'Create and save ArchBro project',
      description: 'Agent-facing initial planning commit. Infer a useful hierarchical Architecture v1 from the user goal even when the prompt does not ask for hierarchy. First author SYSTEM_MAP roots, then recursively evaluate every canonical scope in preorder. Mark a scope EXPANDED when independently addressable architecture responsibilities remain below it; mark JUSTIFIED_LEAF only when no meaningful architecture boundary remains and provide a specific reason. Every SYSTEM_MAP root must be EXPANDED; if the user goal names an atomic service directly, place it beneath an appropriate root boundary. Do not stop at broad multi-responsibility containers such as a backend application, web client, or data platform merely because the prompt was brief. During RECONCILE, author each dependency at the deepest canonical endpoints that actually own the interaction; use root-to-root relationships only for true boundary-level interactions. Hierarchy is expressed by children, never by fabricated containment relationships. ArchBro validates complete scope coverage and commits Architecture v1 atomically.',
      inputSchema: {
        type: 'object',
        properties: {
          name: {type: 'string', minLength: 1, description: 'Project name.'},
          goal: {type: 'string', minLength: 1, description: 'Project goal or brief.'},
          architecture_summary: {type: 'string', minLength: 1, description: 'Short summary of Architecture v1.'},
          components: {
            type: 'array', minItems: 1, maxItems: 6,
            description: 'Hierarchical canonical root components from SYSTEM_MAP. Every root and descendant requires a stable id; children may recursively nest to depth 3.',
            items: architectureComponentInputSchema(1),
          },
          relationships: {
            type: 'array',
            description: 'Explicit interactions authored during RECONCILE. A plan with multiple atomic components must include relationships between distinct components. Do not omit interactions, substitute self-links, or invent containment dependencies.',
            items: {
              type: 'object',
              properties: {
                source: {type: 'string', minLength: 1, description: 'Source component name or stable id. Prefer the deepest canonical component that actually owns the interaction.'},
                target: {type: 'string', minLength: 1, description: 'Target component name or stable id. Prefer the deepest canonical component that actually receives the interaction.'},
                type: {type: 'string', minLength: 1},
                description: {type: 'string'},
              },
              required: ['source', 'target', 'type'],
              additionalProperties: false,
            },
          },
          tasks: {
            type: 'array', minItems: 1,
            items: {
              type: 'object',
              properties: {
                title: {type: 'string', minLength: 1},
                component: {type: 'string', description: 'Related canonical component name or stable id.'},
                description: {type: 'string'},
              },
              required: ['title'],
              additionalProperties: false,
            },
          },
          planning_trace: initialPlanningTraceInputSchema(),
          reasoning: {type: 'string', minLength: 1, description: 'Why this initial plan fits the goal.'},
        },
        required: ['name', 'goal', 'architecture_summary', 'components', 'tasks', 'planning_trace', 'reasoning'],
        additionalProperties: false,
      },
      annotations: {readOnlyHint: false, untrustedContentHint: false},
      execute: async ({name, goal, architecture_summary, components, relationships = [], tasks, planning_trace, reasoning}, client = {}) => asToolResult(
        await bridge.bootstrapProject({name, goal, architectureSummary: architecture_summary, components, relationships, tasks, planningTrace: planning_trace, reasoning, signal: client.signal}),
      ),
    },
    {
      name: `${TOOL_PREFIX}expand_architecture_scope`,
      title: 'Propose one-level architecture scope expansion',
      description: 'Add one explicit child level under an existing canonical component without replacing existing children or stable ids. The expansion becomes a PENDING architecture proposal and still requires human acceptance. To create grandchildren, call this tool again later with the accepted child as the scope.',
      inputSchema: {
        type: 'object',
        properties: {
          scope_component_id: {type: 'string', minLength: 1, description: 'Existing plain canonical component id to expand.'},
          children: {type: 'array', minItems: 1, maxItems: 7, items: expansionChildInputSchema()},
          reasoning: {type: 'string', minLength: 1},
          evidence: {type: 'array', minItems: 1, items: {type: 'string', minLength: 1}},
          impact: {type: 'string'},
          expected_architecture_version: {type: 'integer', minimum: 0},
        },
        required: ['scope_component_id', 'children', 'reasoning', 'evidence', 'expected_architecture_version'],
        additionalProperties: false,
      },
      annotations: {readOnlyHint: false, untrustedContentHint: true},
      execute: async ({scope_component_id, children, reasoning, evidence, impact = '', expected_architecture_version}, client = {}) => asToolResult(
        await bridge.expandArchitectureScope({scopeComponentId: scope_component_id, children, reasoning, evidence, impact, expectedArchitectureVersion: expected_architecture_version, signal: client.signal}),
      ),
    },
    {
      name: `${TOOL_PREFIX}get_architecture_decision_context`,
      title: 'Get Living Architecture decision context',
      description: 'Read accepted architecture, execution state, evidence, and governance rules for an architecture decision.',
      inputSchema: {type: 'object', properties: {}, additionalProperties: false},
      annotations: {readOnlyHint: true, untrustedContentHint: true},
      execute: async (_input, client = {}) => asToolResult(await bridge.getDecisionContext({signal: client.signal})),
    },
    {
      name: `${TOOL_PREFIX}submit_architecture_recommendation`,
      title: 'Submit architecture recommendation',
      description: 'Submit an architecture recommendation with its reasoning and evidence. Proposed architecture changes become pending human-review items and are never auto-approved.',
      inputSchema: {
        type: 'object',
        properties: {
          recommendation: {type: 'string', enum: ['KEEP_CURRENT', 'ACCEPT_PROPOSED_CHANGE']},
          reasoning: {type: 'string', minLength: 1},
          evidence: {type: 'array', minItems: 1, items: {type: 'string', minLength: 1}},
          observed_change: {type: 'string', minLength: 1},
          affected_components: {type: 'array', items: {type: 'string', minLength: 1}},
          proposed_changes: {
            type: 'array',
            description: 'Reviewable architecture operations. Supported forms: replace_component; remove_component; metadata-only update_component; additive one-level expand_scope; replace_relationships. Prefer archbro_expand_architecture_scope for structural decomposition.',
            items: {type: 'object', additionalProperties: true},
          },
          impact: {type: 'string'},
          expected_architecture_version: {type: 'integer', minimum: 0, description: 'Accepted Living Architecture version used to prepare this recommendation.'},
        },
        required: ['recommendation', 'reasoning', 'evidence', 'observed_change', 'expected_architecture_version'],
        additionalProperties: false,
      },
      annotations: {readOnlyHint: false, untrustedContentHint: true},
      execute: async ({recommendation, reasoning, evidence, observed_change, affected_components = [], proposed_changes = [], impact = '', expected_architecture_version}, client = {}) => asToolResult(
        await bridge.submitAgentRecommendation({recommendation, reasoning, evidence, observedChange: observed_change, affectedComponents: affected_components, proposedChanges: proposed_changes, impact, expectedArchitectureVersion: expected_architecture_version, signal: client.signal}),
      ),
    },
    {
      name: `${TOOL_PREFIX}create_task`,
      title: 'Create project task',
      description: 'Create one implementation or execution task within the accepted Living Architecture without invoking ArchBro\'s built-in model. Use this for normal work that does not require an architecture change.',
      inputSchema: {
        type: 'object',
        properties: {
          request_id: {type: 'string', minLength: 1, maxLength: 200, description: 'Stable idempotency key. Reuse the same value when retrying the same create request.'},
          title: {type: 'string', minLength: 1},
          description: {type: 'string'},
          owner: {type: 'string', enum: ['HUMAN', 'AGENT', 'UNASSIGNED']},
          related_component: {type: 'string', minLength: 1, description: 'Accepted canonical component id.'},
          dependencies: {type: 'array', maxItems: 40, items: {type: 'string', minLength: 1}},
          acceptance_criteria: {type: 'array', maxItems: 40, items: {type: 'string', minLength: 1}},
        },
        required: ['request_id', 'title'],
        additionalProperties: false,
      },
      annotations: {readOnlyHint: false, untrustedContentHint: false},
      execute: async ({request_id, title, description = '', owner = 'UNASSIGNED', related_component, dependencies = [], acceptance_criteria = []}, client = {}) => asToolResult(
        await bridge.createTask({requestId: request_id, title, description, owner, relatedComponent: related_component, dependencies, acceptanceCriteria: acceptance_criteria, signal: client.signal}),
      ),
    },
    {
      name: `${TOOL_PREFIX}update_task_status`,
      title: 'Execute ArchBro task transition',
      description: 'Start or complete one existing task through ArchBro\'s deterministic task boundary without invoking the built-in model.',
      inputSchema: {type: 'object', properties: {task_id: {type: 'string', minLength: 1}, status: {type: 'string', enum: ['IN_PROGRESS', 'DONE']}}, required: ['task_id', 'status'], additionalProperties: false},
      annotations: {readOnlyHint: false, untrustedContentHint: false},
      execute: async ({task_id, status}, client = {}) => asToolResult(await bridge.updateTaskStatus({taskId: task_id, status, signal: client.signal})),
    },
    {
      name: `${TOOL_PREFIX}record_project_observation`,
      title: 'Record project observation',
      description: 'Persist external evidence or a project fact as an ArchBro event without treating it as an architecture recommendation and without invoking the built-in model. This never mutates accepted Living Architecture.',
      inputSchema: {
        type: 'object',
        properties: {
          summary: {type: 'string', minLength: 1},
          evidence: {type: 'array', minItems: 1, maxItems: 50, items: {type: 'string', minLength: 1}},
          related_components: {type: 'array', maxItems: 20, items: {type: 'string', minLength: 1}},
          related_task_id: {type: 'string', minLength: 1},
        },
        required: ['summary', 'evidence'],
        additionalProperties: false,
      },
      annotations: {readOnlyHint: false, untrustedContentHint: true},
      execute: async ({summary, evidence, related_components = [], related_task_id}, client = {}) => asToolResult(
        await bridge.recordProjectObservation({summary, evidence, relatedComponents: related_components, relatedTaskId: related_task_id, signal: client.signal}),
      ),
    },
  ];

  return tools;
}

function createConnectedMcpTools(bridge) {
  return [
    {
      name: `${TOOL_PREFIX}list_connected_mcp_servers`,
      title: 'List connected MCP servers',
      description: 'List external MCP sources available to the selected ArchBro project, including user-authorized provider connections and deployment-bound MCP servers.',
      inputSchema: {type: 'object', properties: {}, additionalProperties: false},
      annotations: {readOnlyHint: true, untrustedContentHint: true},
      execute: async (_input, client = {}) => asToolResult(await listConnectedMcpServers(bridge, {signal: client.signal})),
    },
    {
      name: `${TOOL_PREFIX}list_connected_mcp_tools`,
      title: 'List connected MCP tools',
      description: 'Discover tools exposed by one MCP source returned by list_connected_mcp_servers, including authorized provider connections.',
      inputSchema: {type: 'object', properties: {server_id: {type: 'string', minLength: 1}}, required: ['server_id'], additionalProperties: false},
      annotations: {readOnlyHint: true, untrustedContentHint: true},
      execute: async ({server_id}, client = {}) => asToolResult(await listConnectedMcpTools(bridge, {serverId: server_id, signal: client.signal})),
    },
    {
      name: `${TOOL_PREFIX}call_connected_mcp_tool`,
      title: 'Call connected MCP tool',
      description: 'Call one tool from an MCP source returned by list_connected_mcp_servers. Provider output is external evidence, not canonical state. Large results return full_result_ref; pass it as result_ref with offset/max_chars to recover only the needed slice without calling the provider again.',
      inputSchema: {type: 'object', properties: {server_id: {type: 'string', minLength: 1}, tool_name: {type: 'string', minLength: 1}, arguments: {type: 'object', additionalProperties: true}, result_ref: {type: 'string', minLength: 1}, offset: {type: 'integer', minimum: 0}, max_chars: {type: 'integer', minimum: 1, maximum: 12000}}, required: ['server_id', 'tool_name'], additionalProperties: false},
      annotations: {readOnlyHint: false, untrustedContentHint: true},
      execute: async ({server_id, tool_name, arguments: args = {}, result_ref, offset = 0, max_chars = EXTERNAL_RESULT_RECOVERY_CHARS}, client = {}) => asToolResult(await callConnectedMcpTool(bridge, {serverId: server_id, toolName: tool_name, arguments: args, resultRef: result_ref, offset, maxChars: max_chars, signal: client.signal})),
    },
  ];
}

function connectedMcpGatewayConfigured() {
  return Boolean(globalThis.window?.__ARCHBRO_RUNTIME_CONFIG__?.connected_mcp_gateway_configured);
}

export function createArchBroTools(bridge, {includeConnectedMcp = true} = {}) {
  const tools = includeConnectedMcp
    ? [...createCoreTools(bridge), ...createConnectedMcpTools(bridge)]
    : createCoreTools(bridge);
  return tools.map(compactToolForModel);
}

function resolveModelContext(modelContext) {
  if (modelContext && typeof modelContext.registerTool === 'function') return modelContext;
  const documentModelContext = globalThis.document?.modelContext;
  if (documentModelContext && typeof documentModelContext.registerTool === 'function') return documentModelContext;

  const navigatorModelContext = globalThis.navigator?.modelContext;
  if (!navigatorModelContext || typeof navigatorModelContext.registerTool !== 'function') return null;

  if (globalThis.document && !globalThis.document.modelContext) {
    try {
      Object.defineProperty(globalThis.document, 'modelContext', {configurable: true, value: navigatorModelContext});
    } catch {
      // Some hosts expose the legacy native surface without allowing page-side aliasing.
    }
  }
  return navigatorModelContext;
}

export async function registerArchBroWebMCP({modelContext, bridge, signal, includeConnectedMcp} = {}) {
  const resolvedModelContext = resolveModelContext(modelContext);
  const resolvedBridge = bridge ?? globalThis.window?.ArchBroWebBridge;
  if (!resolvedModelContext) throw new Error('WebMCP is unavailable: document.modelContext.registerTool() / navigator.modelContext.registerTool() was not found');
  const tools = createArchBroTools(resolvedBridge, {includeConnectedMcp});
  for (const tool of tools) await resolvedModelContext.registerTool(tool, signal ? {signal} : undefined);
  return tools;
}


export async function autoRegisterArchBroWebMCP() {
  const modelContext = resolveModelContext();
  if (!modelContext || !globalThis.window?.ArchBroWebBridge) return {registered: false, reason: 'webmcp-or-bridge-unavailable'};
  const controller = new AbortController();
  const initialRuntime = await verifyWebMcpRuntime({signal: controller.signal, autoReload: true});
  if (initialRuntime.stale_client) return {registered: false, reason: 'stale-webmcp-client-reloading'};
  const tools = await registerArchBroWebMCP({modelContext, bridge: globalThis.window.ArchBroWebBridge, signal: controller.signal});
  const checkRuntime = () => {
    verifyWebMcpRuntime({signal: controller.signal, autoReload: true}).catch((error) => {
      if (!controller.signal.aborted) console.warn('[archbro-webmcp] runtime identity check failed', error);
    });
  };
  const intervalId = globalThis.window.setInterval(checkRuntime, WEBMCP_RUNTIME_CHECK_INTERVAL_MS);
  const focusHandler = () => checkRuntime();
  const visibilityHandler = () => { if (!globalThis.document.hidden) checkRuntime(); };
  globalThis.window.addEventListener('focus', focusHandler);
  globalThis.document.addEventListener('visibilitychange', visibilityHandler);
  globalThis.window.ArchBroWebMCP = {
    surfaceVersion: WEBMCP_SURFACE_VERSION,
    assetSha256: initialRuntime.manifest.asset_sha256,
    tools: tools.map(({name, title, description}) => ({name, title, description})),
    dispose: () => {
      controller.abort();
      globalThis.window.clearInterval(intervalId);
      globalThis.window.removeEventListener('focus', focusHandler);
      globalThis.document.removeEventListener('visibilitychange', visibilityHandler);
    },
  };
  return {registered: true, count: tools.length};
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  queueMicrotask(() => { autoRegisterArchBroWebMCP().catch((error) => { console.warn('[archbro-webmcp] registration failed', error); }); });
}
