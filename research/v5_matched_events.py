"""Retrospective same-event comparison of frozen maps, never final qualification."""
import argparse
import json
from pathlib import Path
import sys
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from channel_review import prepare, continuous_map
from continuous_maps import detail
from flutter_planning import read_channel, write_json
from flutter_tracking import evaluate_events
from flutter_policy import digest


def compare(v4, v5, channel, start, duration):
    variants = {}
    events = {}
    for name, source in (('v4', v4), ('v5', v5)):
        path, plan, row, regions, origin, _, frames = prepare(source, channel, start, duration)
        tt, nominal, *_ = continuous_map(path.parent, regions, channel, origin, frames, row['sample_rate'])
        record = detail(path.parent, regions[0])
        for event in record.get('final_holdouts', {}).get(str(channel), []):
            key = round(event['start_seconds'], 3)
            events.setdefault(key, dict(event=event, reserved_under=[]))['reserved_under'].append(name)
        variants[name] = dict(path=path, row=row, origin=origin, tt=tt, nominal=nominal)
    measurements = []
    for key, item in sorted(events.items()):
        event = item['event']
        if event['start_seconds'] < start+.5 or event['end_seconds'] > start+duration-.5:
            continue
        row = variants['v5']['row']
        sr = row['sample_rate']
        low = row['start_seconds']+round((event['start_seconds']-.5-row['start_seconds'])*sr)/sr
        high = event['end_seconds']+.5
        x, _, source_hash = read_channel(row, low, high)
        local_event = dict(event, start=event['start_seconds']-low, end=event['end_seconds']-low)
        result = dict(start_seconds=event['start_seconds'], reserved_under=item['reserved_under'],
            source_excerpt_sha256=source_hash, identity=evaluate_events(x, sr, None, [local_event], sample_positions=np.arange(len(x), dtype=float)))
        for name, value in variants.items():
            output_times = low-value['origin']+np.arange(len(x))/sr
            input_times = np.interp(output_times, value['nominal'], value['tt'])
            positions = (input_times-(low-value['origin']))*sr
            result[name] = evaluate_events(x, sr, None, [local_event], sample_positions=positions)
        measurements.append(result)
    return dict(channel=channel, start=start, duration=duration,
        plans={name:dict(path=str(v['path']), sha256=digest(v['path'])) for name,v in variants.items()},
        scope='Retrospective development comparison of identical events through exact frozen maps. The event union is reserved under at least one method, but may have been used for training by the other. Not independent head-to-head final evaluation; no tuning or refitting.',
        events=measurements)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('v4', type=Path); parser.add_argument('v5', type=Path)
    parser.add_argument('channel', type=int); parser.add_argument('start', type=float)
    parser.add_argument('duration', type=float); parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a fresh comparison output')
    write_json(args.output, compare(args.v4,args.v5,args.channel,args.start,args.duration))
