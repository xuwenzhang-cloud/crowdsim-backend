"""ARDE evidence only; never infer runtime equality from equal final metrics."""
from copy import deepcopy
import hashlib
import json
import math


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class AuditGateway:
    def __init__(self, gateway, owner):
        self.gateway, self.owner, self.pending = gateway, owner, {}

    def __getattr__(self, name):
        return getattr(self.gateway, name)

    def submit(self, payload):
        request = self.gateway.submit(payload)
        active = self.owner._active
        if (active and active['experiment']['scenario'].get('ardeRequirementSnapshot')
                and not active.get('accept_frames', False) and payload.get('action') in {'reset', 'set_event', 'start'}):
            self.pending[request] = (active, deepcopy(payload))
        return request

    def wait_for_ack(self, request, **kwargs):
        try:
            result = self.gateway.wait_for_ack(request, **kwargs)
            tracked = self.pending.get(request)
            if tracked:
                self.owner.audit_ack(*tracked, result)
            return result
        finally:
            self.pending.pop(request, None)


def peak_context(observations, decisions, acks=(), evaluation_window=30):
    points = [row.get('payload', row) for row in observations]
    points = [p for p in points if type(p.get('global', {}).get('maxDensity')) in (int, float)
              and math.isfinite(p['global']['maxDensity'])]
    if not points:
        return None
    peak = max(points, key=lambda p: p['global']['maxDensity'])
    density = peak['global']['maxDensity']
    # Only name locations with an equal measured density; a hotspot maximum
    # cannot stand in for the global maximum measured on a different geometry.
    locations = []
    for key, identifier in [('edges', 'edgeId'), ('regions', 'regionId')]:
        for item in peak.get(key, []):
            value = item.get('density')
            if type(value) in (int, float) and math.isclose(value, density, rel_tol=1e-6, abs_tol=1e-9):
                locations.append({'kind': key, 'id': item.get(identifier)})
    prior = []
    samples = sorted(points, key=lambda p: p['simTimeSeconds'])
    receipts = [row.get('payload', row) for row in acks]
    for row in decisions:
        decision = row.get('payload', row)
        # Link via observationId, not a nonexistent decision simTimeSeconds.
        point = next((p for p in points if p.get('observationId') == decision.get('observationId')), None)
        if point and point['simTimeSeconds'] <= peak['simTimeSeconds']:
            after = next((p for p in samples if p['simTimeSeconds'] >= point['simTimeSeconds'] + evaluation_window), None)
            before_values = deepcopy(point.get('global', {}))
            after_values = deepcopy(after.get('global', {})) if after else None
            deltas = {}
            if after:
                for key in ('maxDensity', 'meanSpeed', 'riskIndex', 'completionRate'):
                    old, new = before_values.get(key), after_values.get(key)
                    if type(old) in (int, float) and type(new) in (int, float) and math.isfinite(old) and math.isfinite(new):
                        deltas[key] = new - old
            prior.append({'timeSeconds': point['simTimeSeconds'], 'decisionId': decision.get('decisionId'),
                          'actions': deepcopy(decision.get('actions', [])),
                          'receipts': [deepcopy(a) for a in receipts if a.get('decisionId') == decision.get('decisionId')],
                          'before': before_values, 'after': after_values,
                          'afterTimeSeconds': after.get('simTimeSeconds') if after else None,
                          'windowSeconds': evaluation_window, 'deltas': deltas,
                          'regionBefore': deepcopy(point.get('regions', [])),
                          'regionAfter': deepcopy(after.get('regions', [])) if after else None})
    return {'density': density, 'timeSeconds': peak['simTimeSeconds'], 'locations': locations,
            'locationStatus': 'measured' if locations else 'not_available',
            'precedingDecisions': sorted(prior, key=lambda d: d['timeSeconds'])[-3:],
            'interpretation': 'temporal association, not proof of action causality'}
