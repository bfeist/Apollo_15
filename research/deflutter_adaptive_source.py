"""Bounded in-memory original-sample extraction with real source context.

No denoising cache is written. A missing/ambiguous electrical pilot preserves
the original; clipping/context failures are withheld rather than fabricated.
"""
import numpy as np
from deflutter_adaptive_mains import remove_adaptive_comb
from deflutter_campaign import load_excerpt,sha

VERSION='adaptive-original-measurement-research-v1'


class SourceWithheld(ValueError):
    def __init__(self,reason,metadata=None):
        super().__init__(reason);self.metadata=metadata


def derive_original(x,sr,start_seconds,end_seconds,progress=None):
    if start_seconds<30 or len(x)/sr-end_seconds<30:
        raise SourceWithheld('Insufficient real source context: need>=30s on both sides')
    clips=int(np.sum(abs(x)>=.999))
    if clips:raise SourceWithheld('Original clipping in transform dependency context',dict(original_clip_samples=clips))
    derived,meta=remove_adaptive_comb(x,sr,sr/4,sr/4,progress=progress)
    core=derived[round(start_seconds*sr):round(end_seconds*sr)].copy()
    return core,dict(methodology=VERSION,extraction=meta,original_clip_samples=clips,
        core_offsets_seconds=[start_seconds,end_seconds],original_unchanged=True,
        decoded_sha256=sha(core.tobytes()),predictor_response_band='2150..2850Hz remains excluded by candidate capture gate')


def load_adaptive_original(inv,row,start,end,progress=None):
    context=40.
    if start-context<row['start_seconds'] or end+context>row['end_seconds']:
        raise SourceWithheld('Complete40s source context unavailable; do not pad/extrapolate')
    original,sr,parent=load_excerpt(inv,row,start-context,end+context,'original')
    try:core,meta=derive_original(original,sr,context,context+end-start,progress=progress)
    except SourceWithheld as exc:
        raise SourceWithheld(str(exc),dict(parent_source=parent,parent_extent_seconds=[start-context,end+context],detail=exc.metadata)) from exc
    return core,sr,dict(path=parent['path'],source_identity=parent['source_identity'],
        decoded_sha256=meta['decoded_sha256'],version='adaptive_original',
        parent_source=parent,parent_extent_seconds=[start-context,end+context],
        measurement_extent_seconds=[start,end],derived_measurement=meta)
