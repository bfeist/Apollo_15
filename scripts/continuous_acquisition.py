"""Bounded acquisition evidence that survives monitoring and I/O boundaries.

This ledger schedules evidence; it never approves a correction. Target-channel
events receive immutable roles before any candidate is scored. No observations
from different channels are pooled as independent votes. The caller must track
the proposed interval and prove continuous reference support across its training
and validation events before using the returned roles.
"""
from copy import deepcopy
import math


DEFAULT_HORIZONS = (60., 120., 180., 300.)
ROLE_CYCLE = ('calibration', 'calibration', 'validation', 'validation', 'final')


class AcquisitionEvidence:
    """Per-channel, serializable, bounded evidence and fresh-attempt scheduler."""
    def __init__(self, *, horizons=DEFAULT_HORIZONS, minimum_per_split=2,
                 independent_seconds=3., context_seconds=4.):
        horizons = tuple(float(value) for value in horizons)
        if (not horizons or any(not math.isfinite(v) or v <= 0 for v in horizons)
                or tuple(sorted(set(horizons))) != horizons
                or not isinstance(minimum_per_split, int) or minimum_per_split < 1
                or not math.isfinite(independent_seconds) or independent_seconds <= 0
                or not math.isfinite(context_seconds) or context_seconds < 0):
            raise ValueError('Use increasing finite acquisition horizons and positive evidence limits')
        self.horizons = horizons
        self.minimum_per_split = minimum_per_split
        self.independent_seconds = float(independent_seconds)
        self.context_seconds = float(context_seconds)
        self.channels = {}

    def _channel(self, channel):
        return self.channels.setdefault(str(channel), dict(events=[], next_index=0,
            last_event_start=None, attempted_through=None, rejected_nonindependent=0))

    def event_role(self, event, index):
        return ROLE_CYCLE[index % len(ROLE_CYCLE)]

    def ingest(self, channel, events, *, origin=0.):
        """Accept chronological original-tape events, returning only new events.

        ``start/end`` are relative to ``origin``; explicit ``start_seconds`` and
        ``end_seconds`` are already tape coordinates. Repeated context detections
        and margins on one beep cannot become new independent observations.
        """
        if not math.isfinite(origin):
            raise ValueError('Event origin must be finite')
        converted = []
        for event in events:
            a = float(event.get('start_seconds', origin+event.get('start', 0.)))
            b = float(event.get('end_seconds', origin+event.get('end', 0.)))
            if not math.isfinite(a) or not math.isfinite(b) or b <= a:
                raise ValueError('Events need finite increasing original coordinates')
            converted.append(dict(event, start_seconds=a, end_seconds=b))
        row, accepted = self._channel(channel), []
        for event in sorted(converted, key=lambda e: (e['start_seconds'], e['end_seconds'])):
            start = event['start_seconds']
            if row['last_event_start'] is not None and start-row['last_event_start'] < self.independent_seconds:
                row['rejected_nonindependent'] += 1
                continue
            index = row['next_index']
            item = dict(event, channel=channel, evidence_index=index,
                        role=self.event_role(event, index))
            # Retain one unambiguous coordinate system in checkpoint state.
            item.pop('start', None)
            item.pop('end', None)
            row['events'].append(item)
            row['next_index'] += 1
            row['last_event_start'] = start
            accepted.append(deepcopy(item))
        if accepted:
            cutoff = accepted[-1]['end_seconds']-self.horizons[-1]-self.context_seconds
            row['events'] = [e for e in row['events'] if e['end_seconds'] >= cutoff]
        return accepted

    def propose(self, channel, end, *, available_start=0., available_end=None):
        """Return the shortest evidence-ready acquisition interval, or wait.

        The result's roles use absolute tape coordinates. ``local_roles`` below
        converts them for existing evaluators. A ready interval is only a search
        opportunity, never numeric support or production acceptance.
        """
        if not math.isfinite(end) or not math.isfinite(available_start) or end <= available_start:
            raise ValueError('Acquisition coverage must be finite and nonempty')
        if available_end is not None and (not math.isfinite(available_end) or end > available_end):
            raise ValueError('Evidence extends past original source coverage')
        row = self._channel(channel)
        floor = available_start if row['attempted_through'] is None else max(available_start, row['attempted_through'])
        counts = {}
        for horizon in self.horizons:
            start = max(floor, end-horizon)
            selected = [e for e in row['events'] if e['start_seconds'] >= start and e['end_seconds'] <= end]
            roles = {name: [deepcopy(e) for e in selected if e['role'] == name]
                     for name in ('calibration', 'validation', 'final')}
            counts[str(horizon)] = {name: len(value) for name, value in roles.items()}
            if min(len(roles['calibration']), len(roles['validation'])) < self.minimum_per_split:
                continue
            development = roles['calibration']+roles['validation']
            support_start = min(e['start_seconds'] for e in development)
            support_end = max(e['end_seconds'] for e in development)
            return dict(status='ready_for_search', channel=channel, start_seconds=start,
                        end_seconds=end, horizon_seconds=horizon,
                        analysis_start=max(available_start, start-self.context_seconds),
                        analysis_end=end+self.context_seconds if available_end is None else min(available_end, end+self.context_seconds),
                        required_support_start=support_start, required_support_end=support_end,
                        roles=roles, counts_by_horizon=counts, attempted_through=row['attempted_through'],
                        oscillator_feed_group='unknown', independent_oscillators=False,
                        correction_approved=False)
        return dict(status='waiting_for_independent_evidence', channel=channel,
                    start_seconds=max(floor, end-self.horizons[-1]), end_seconds=end,
                    counts_by_horizon=counts, attempted_through=row['attempted_through'],
                    correction_approved=False)

    def mark_attempt(self, channel, end):
        """Consume a frozen candidate attempt, including failures, before retry."""
        if not math.isfinite(end):
            raise ValueError('Attempt endpoint must be finite')
        row = self._channel(channel)
        if row['attempted_through'] is not None and end < row['attempted_through']:
            raise ValueError('An acquisition attempt cannot move backward')
        row['attempted_through'] = float(end)
        row['events'] = [e for e in row['events'] if e['start_seconds'] >= end]

    def state(self):
        return deepcopy(dict(version=1, horizons=list(self.horizons),
            minimum_per_split=self.minimum_per_split, independent_seconds=self.independent_seconds,
            context_seconds=self.context_seconds, channels=self.channels))

    @classmethod
    def restore(cls, value):
        if value.get('version') != 1:
            raise ValueError('Unsupported acquisition evidence checkpoint')
        result = cls(horizons=value['horizons'], minimum_per_split=value['minimum_per_split'],
                     independent_seconds=value['independent_seconds'], context_seconds=value['context_seconds'])
        result.channels = deepcopy(value['channels'])
        return result


class SynchronizedEvidence(AcquisitionEvidence):
    """Conservative global role assignment to potentially shared coincident events.

    Coincidence is not evidence of common origin; it prevents the same response
    appearing in different roles. Each channel retains its own independence gate.
    """
    coincidence_seconds = .5

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.global_events = []
        self.global_index = 0
        self.ambiguous_coincidences = 0

    def event_role(self, event, index):
        return event['_synchronized_role']

    def ingest_batch(self, batches):
        pending = []
        for channel, events, origin in batches:
            for event in events:
                a = float(event.get('start_seconds', origin+event.get('start', 0.)))
                b = float(event.get('end_seconds', origin+event.get('end', 0.)))
                if not math.isfinite(a) or not math.isfinite(b) or b <= a:
                    raise ValueError('Events need finite increasing original coordinates')
                pending.append((a, b, channel, event))
        accepted = {channel: [] for channel, _, _ in batches}
        for a, b, channel, event in sorted(pending, key=lambda e:(e[0], e[1], str(e[2]))):
            nearby = [g for g in self.global_events if any(abs(t-a) <= self.coincidence_seconds
                      for t in g.get('members', [g['start_seconds']]))]
            if len({g['role'] for g in nearby}) > 1:
                # A late bridging detection cannot change already frozen roles.
                self.ambiguous_coincidences += 1
                continue
            if nearby:
                group = min(nearby, key=lambda g:(abs(g['start_seconds']-a), g['index']))
                group['members'].append(a)
            else:
                group = dict(start_seconds=a, members=[a], index=self.global_index,
                             role=ROLE_CYCLE[self.global_index % len(ROLE_CYCLE)])
                self.global_events.append(group)
                self.global_index += 1
            assigned = dict(event, start_seconds=a, end_seconds=b,
                            _synchronized_role=group['role'], synchronized_event_index=group['index'])
            accepted[channel].extend(self.ingest(channel, [assigned], origin=0.))
        # Keep the most recent grouping anchors for repeated boundary detections.
        if pending:
            cutoff = max(e[1] for e in pending)-self.horizons[-1]-self.context_seconds-self.coincidence_seconds
            self.global_events = [g for g in self.global_events if g['start_seconds'] >= cutoff]
        return accepted

    def state(self):
        result = super().state()
        result.update(version=2, global_events=deepcopy(self.global_events), global_index=self.global_index,
                      coincidence_seconds=self.coincidence_seconds, ambiguous_coincidences=self.ambiguous_coincidences,
                      coincidence_interpretation='Potential shared event, not authenticated common source')
        return result

    @classmethod
    def restore(cls, value):
        if value.get('version') != 2 or value.get('coincidence_seconds') != cls.coincidence_seconds:
            raise ValueError('Incompatible synchronized acquisition evidence checkpoint')
        result = cls(horizons=value['horizons'], minimum_per_split=value['minimum_per_split'],
                     independent_seconds=value['independent_seconds'], context_seconds=value['context_seconds'])
        result.channels = deepcopy(value['channels'])
        result.global_events = deepcopy(value['global_events'])
        result.global_index = value['global_index']
        result.ambiguous_coincidences = value['ambiguous_coincidences']
        return result


def local_roles(proposal, origin):
    """Convert immutable tape-coordinate roles to an evaluator's excerpt origin."""
    if proposal.get('status') != 'ready_for_search' or not math.isfinite(origin):
        raise ValueError('Need a ready proposal and finite original-coordinate origin')
    return tuple([dict(e, start=e['start_seconds']-origin, end=e['end_seconds']-origin)
                  for e in proposal['roles'][name]]
                 for name in ('calibration', 'validation', 'final'))
