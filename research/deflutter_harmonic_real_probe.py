"""Source-feature availability only; no frozen-map outcome or promotion."""
from deflutter_campaign import ROOT,read
from deflutter_finite_real_probe import run
from deflutter_response_harmonic_pilot import remove_harmonic_pilot_envelopes


if __name__=='__main__':
    controls=read(ROOT/'iterations/I047_harmonic_pilot_controls/metrics.json')
    if controls['negative_validation_accepts'] or controls['false_complete_accepts'] or not controls['easy_positive_pass']:
        raise ValueError('Harmonic pilot development prerequisite failed')
    run(measurement_function=remove_harmonic_pilot_envelopes,iteration='I048_harmonic_real_inputs',
        hypothesis='A bounded off-event harmonic-pilot catalog may supply an electrical nuisance-measurement input where the fundamental failed; source-feature diagnostic only, no new real timing-error outcome')
