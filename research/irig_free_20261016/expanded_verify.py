"""Read-only checks of study outputs; writes only its verification record."""
from pathlib import Path
import ast,hashlib,json,re
ROOT=Path(__file__).resolve().parents[2];D=ROOT/'reports/bespoke_expanded_20261017'

def main():
    def invalid(value):raise ValueError('Nonfinite JSON token '+value)
    count=0
    for p in D.rglob('*.json'):
        if 'plot_config' in str(p):continue
        json.loads(p.read_text(encoding='utf-8'),parse_constant=invalid);count+=1
    inv=json.loads((D/'frozen_inventory.json').read_text());eligible=[t for t in inv if t['status']=='eligible_canonical_snapshot']
    for t in eligible:
        folder=D/t['tape'];s=json.loads((folder/'survey.json').read_text());v=json.loads((folder/'validation.json').read_text());f=json.loads((folder/'followup.json').read_text());part=json.loads((folder/'partial_analysis.json').read_text())
        assert len(s['windows'])==3 and all(len(w['sources'])==30 for w in s['windows']) and len(part)==2
        assert len(v['search'])<=6 and len(v['windows'])<=2
        prior=[w['start'] for w in s['windows']]+[w['start'] for w in v['search']]
        for w in f.get('confirmation',[]):
            assert all(abs(w['start']-p)>=35 for p in prior)
            assert w['reference']==f['selected']['reference'] and w['mode']==f['selected']['mode']
            assert all(o['channel']!=w['reference']['channel'] for o in w['outcomes'])
    original=json.loads((ROOT/'research/archives/latest_consolidation.json').read_text())
    approved=[]
    for r in original['approved_examples_verified']:
        p=ROOT/'reports/cyclic_flutter_20261017'/r['file'];assert hashlib.sha256(p.read_bytes()).hexdigest()==r['sha256'];approved.append(r['file'])
    listening=json.loads((D/'listening.json').read_text())
    for r in listening:
        assert r['listening_approved'] is False
        for f in r['files'].values():assert hashlib.sha256((D/f['file']).read_bytes()).hexdigest()==f['sha256']
    links=re.findall(r'(?:src|href)=[\"\x27]([^\"\x27]+)',(D/'index.html').read_text(encoding='utf-8'))
    assert all((D/link).exists() for link in links if '://' not in link)
    scripts=['bespoke_expanded.py','expanded_partials.py','expanded_followup.py','expanded_synthetic.py','expanded_report.py','expanded_verify.py']
    for name in scripts:ast.parse((ROOT/'research/irig_free_20261016'/name).read_text(encoding='utf-8'))
    result=dict(eligible_tapes_complete=len(eligible),strict_json_files=count,approved_examples_unchanged=approved,new_audio_files_verified=sum(len(r['files']) for r in listening),listening_links_valid=len(links),research_scripts_parsed=len(scripts),fresh_windows_disjoint_from_calibration_and_discovery=True,selected_method_frozen_for_fresh_windows=True,limitations='No listening approval, remote completeness, whole-tape transfer or independent physical-event count established.')
    (D/'verification.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))

if __name__=='__main__':main()
