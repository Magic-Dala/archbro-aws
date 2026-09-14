"""Read-only canvas disclosure over existing canonical relationships.

The general overview uses the backend's existing deterministic backbone rule.
Reference journeys are authored in the frozen v1 corpus and activate only for
an exact architecture match. No node name, browser layout or generated story
is allowed to invent a business route.
"""
from __future__ import annotations

import hashlib
import json

from .diagram import map_edge_ids

REFERENCE_READING_VIEWS = {'22b3cb6e15718150b01195d69143140b21b157f24e350b6fccd058b8328d85b1': [{'id': '01-commerce:1',
                                                                       'label': '顧客結帳與付款',
                                                                       'path': ['cart_ui',
                                                                                'checkout_api',
                                                                                'payment_router',
                                                                                'psp']},
                                                                      {'id': '01-commerce:2',
                                                                       'label': '出货到顧客通知',
                                                                       'path': ['shipping_service',
                                                                                'carrier',
                                                                                'delivery_events',
                                                                                'event_bus',
                                                                                'notification',
                                                                                'email']},
                                                                      {'id': '01-commerce:3',
                                                                       'label': '付款異常到營運處理',
                                                                       'path': ['payment_router',
                                                                                'telemetry',
                                                                                'metrics']}],
 '4a36e5171628c9bf074906e24a07c7ebb1a731bde1f2816df0060400254ec19c': [{'id': '02-ai-workspace:1',
                                                                       'label': '企業登入後執行 AI 工作',
                                                                       'path': ['web',
                                                                                'gateway',
                                                                                'job_api',
                                                                                'job_queue',
                                                                                'scheduler',
                                                                                'agent_worker',
                                                                                'model_gateway']},
                                                                      {'id': '02-ai-workspace:2',
                                                                       'label': '文件修訂到即時同步',
                                                                       'path': ['editor',
                                                                                'document_service',
                                                                                'activity_bus',
                                                                                'sync_service',
                                                                                'websocket']},
                                                                      {'id': '02-ai-workspace:3',
                                                                       'label': '高風險工具到人工審查',
                                                                       'path': ['agent_worker',
                                                                                'tool_gateway',
                                                                                'policy_engine',
                                                                                'approval_service',
                                                                                'audit_writer',
                                                                                'audit_store']}],
 'ce5551f7ec816d8b1ba3d162241de944cbeda4f2ebf8ce6b6744da4d60804a80': [{'id': '03-manufacturing:1',
                                                                       'label': '排程到現場設備執行',
                                                                       'path': ['scheduler',
                                                                                'mes_dispatch',
                                                                                'edge_gateway',
                                                                                'plc']},
                                                                      {'id': '03-manufacturing:2',
                                                                       'label': '品质放行到客戶交付',
                                                                       'path': ['quality_gate',
                                                                                'shipping',
                                                                                'carrier']},
                                                                      {'id': '03-manufacturing:3',
                                                                       'label': '設備實績到數位分身回饋',
                                                                       'path': ['edge_gateway',
                                                                                'stream_ingest',
                                                                                'twin_engine',
                                                                                'scheduler',
                                                                                'simulation']}]}


def canvas_reading_views(architecture, diagram):
    canonical = json.dumps(architecture.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    common = {"architecture_version": architecture.version, "architecture_sha256": fingerprint}
    views = [{**common, "id": "backbone", "label": "Project backbone",
              "kind": "STRUCTURAL_SUMMARY", "source": "archbro.map-backbone.v1",
              "edge_ids": sorted(map_edge_ids(diagram))}]
    for authored in REFERENCE_READING_VIEWS.get(fingerprint, []):
        steps = []
        for source, target in zip(authored["path"], authored["path"][1:]):
            matches = sorted(edge.id for edge in diagram.edges if edge.source == f"node:{source}" and edge.target == f"node:{target}")
            # A node-only itinerary cannot choose between different parallel
            # business actions. Withhold an ambiguous view instead of guessing.
            if len(matches) != 1:
                break
            steps.append({"source": f"node:{source}", "target": f"node:{target}", "edge_id": matches[0]})
        else:
            sample, scenario = authored["id"].split(":")
            views.append({**common, "id": authored["id"], "label": authored["label"],
                          "kind": "AUTHORED_JOURNEY",
                          "source": f"reference-projects/v1/{sample}.json#scenarios/{int(scenario)-1}",
                          "edge_ids": sorted({step["edge_id"] for step in steps}), "steps": steps,
                          "node_ids": [f"node:{node}" for node in authored["path"]]})
    return views
