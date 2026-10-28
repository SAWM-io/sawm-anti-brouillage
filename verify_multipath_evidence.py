#!/usr/bin/env python3
"""Independent read-only check of a completed, private multipath evidence bundle.

No laboratory code is imported. Raw payloads, credentials, host configuration
and command lines are never printed. SQLite is opened in read-only mode.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import math
from pathlib import Path
import sqlite3


class InvalidEvidence(ValueError):
    pass


def require(condition, label):
    if not condition:
        raise InvalidEvidence(label)


def read_json(path):
    return json.loads(path.read_text())


def verify(data_dir, source_dir=None):
    root=Path(data_dir)
    source=Path(source_dir) if source_dir else Path(__file__).resolve().parent
    report=read_json(root/'report.json')
    exported=read_json(root/'attempts.json')
    n=report['workload']['chunks_per_direction']; size=report['workload']['bytes_per_chunk']
    require(type(n) is int and 1<=n<=1000 and size==4096,'workload_bounds')
    require(report.get('policy') in ('adaptive','fixed_a') and report.get('scenario') in ('standard','staggered'),
            'fixed_policy_and_scenario_labels')
    profiles={'nominal','cut_a','cut_ab','only_b','only_c','only_d','degraded_c','all_blocked','recovery'}
    require(all(p.get('profile') in profiles and set(p.get('paths',{}))==set('abcd')
                for p in report['phase_events']),'fixed_phase_and_path_labels')
    outputs={}; inputs={}; attempts={}; counts={}; phases=[]
    for node in ('left','right'):
        db=sqlite3.connect((root/(node+'.sqlite')).resolve().as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        try:
            outputs[node]={r['idx']:dict(r) for r in db.execute('SELECT * FROM outbox')}
            inputs[node]={r['idx']:dict(r) for r in db.execute('SELECT * FROM inbox')}
            attempts[node]=[dict(r) for r in db.execute('SELECT * FROM attempts ORDER BY id')]
        finally:
            db.close()
        require(set(outputs[node])==set(range(n)) and set(inputs[node])==set(range(n)),'all_indices_present')
        require(sum(r['attempts'] for r in outputs[node].values())==len(attempts[node]),'attempt_total')
        keys=('message_id','path','started_ms','ended_ms','outcome','selection_reason')
        require([{k:r[k] for k in keys} for r in attempts[node]]==exported[node],'attempt_export_matches_sqlite')
    for node,peer in (('left','right'),('right','left')):
        chunks=[]
        for index in range(n):
            row=outputs[node][index]; body=json.loads(row['body'])
            data=base64.b64decode(body['data'],validate=True); chunks.append(data)
            require(len(data)==size and hashlib.sha256(data).hexdigest()==body['sha256'],'source_chunk_digest')
            seed=hashlib.sha256(f'SAWM synthetic file / {node} / {index}'.encode()).digest()
            require(data==(seed*129)[:size],'synthetic_source_matches_specification')
            require(body['run_id']==report['run_id'] and body['sender']==node and body['index']==index
                    and body['total']==n and body['message_id']==f'{node}-{index:04d}','source_identity')
            stored=inputs[peer][index]
            require(stored['message_id']==body['message_id'] and bytes(stored['data'])==data
                    and stored['sha256']==body['sha256'],'opposite_receiver_matches_source')
            own=[r for r in attempts[node] if r['message_id']==body['message_id']]
            successes=[r for r in own if r['outcome']=='acked']
            require(len(own)==row['attempts'] and len(successes)==1,'exactly_one_verified_ack_per_message')
            success=successes[0]; receipt=json.loads(success['receipt'])
            expected=dict(committed=True,duplicate=receipt['duplicate'],message_id=body['message_id'],
                          receiver=peer,run_id=body['run_id'],sender=node,sha256=body['sha256'])
            require(receipt==expected and receipt['committed'] is True and type(receipt['duplicate']) is bool,'receipt_identity_digest_commit')
            require(row['ack_ms']==success['ended_ms'] and stored['received_ms']<=success['ended_ms'],'receipt_after_commit')
        expected=b''.join(chunks); binary=(root/f'received-{peer}.bin').read_bytes()
        require(binary==expected,'received_binary_equals_source')
        digest=hashlib.sha256(binary).hexdigest(); result=report['node_results'][peer]
        require(digest==result['received_file_sha256']==result['expected_file_sha256'],'report_file_digest')
        require(result['received_bytes']==len(binary) and result['received_chunks']==n,'report_counts')
        counts[node]=dict(chunks=n,bytes=len(binary),attempts=len(attempts[node]),verified_acks=n,received_sha256=digest)
    for index,phase in enumerate(report['phase_events']):
        start=phase['applied_ms']+200
        end=report['phase_events'][index+1]['started_ms'] if index+1<len(report['phase_events']) else report['elapsed_ms']
        verified={'profile':phase['profile'],'directions':{}}
        for node,peer in (('left','right'),('right','left')):
            success=[r for r in attempts[node] if r['outcome']=='acked' and r['started_ms']>=start and r['ended_ms']<end]
            fresh=[r for r in inputs[peer].values() if start<=r['received_ms']<end]
            metric=report['phase_metrics'][index]['directions'][node]
            require(metric['new_acked']==len(success) and metric['new_effects']==len(fresh)
                    and metric['new_bytes']==len(fresh)*size,'phase_metrics_match_raw_evidence')
            require(all(phase['paths'][r['path']]['loss_pct']<100 for r in success),'no_ack_on_blocked_path')
            if phase['profile']=='all_blocked':
                require(not success and not fresh,'no_transfer_during_total_outage')
            elif report['policy']=='adaptive':
                require(bool(success) and bool(fresh),'fresh_bidirectional_transfer_in_each_available_phase')
            verified['directions'][node]=dict(new_chunks=len(fresh),acks=len(success),paths=sorted({r['path'] for r in success}))
        phases.append(verified)
    logs=read_json(root/'setup-cleanup-private.json')
    commands=[r for r in logs if 'argv' in r]; reads=[r for r in logs if 'readback' in r]
    require(all(r['returncode']==0 for r in commands),'system_commands_succeeded')
    require(len(reads)==len(report['phase_events']),'kernel_readbacks_complete')
    kernel_count=0
    for phase,read in zip(report['phase_events'],reads):
        require(phase['profile']==read['phase'],'kernel_readback_phase')
        for node in ('left','right'):
            for path,expected in phase['paths'].items():
                rows=[r['options'] for r in read['readback'][node+'_'+path] if r['kind']=='netem']
                require(len(rows)==1,'netem_present')
                value=rows[0]
                require(math.isclose(value['delay']['delay']*1000,expected['delay_ms'],abs_tol=.01)
                        and math.isclose(value['delay']['jitter']*1000,expected['jitter_ms'],abs_tol=.01)
                        and math.isclose(value.get('loss-random',{}).get('loss',0)*100,expected['loss_pct'],abs_tol=.01),
                        'kernel_fault_parameters_match')
                kernel_count+=1
    hashes={name:hashlib.sha256((source/name).read_bytes()).hexdigest() for name in ('multipath_policy.py','multipath_vm_lab.py')}
    require(hashes==report['source_sha256'],'tested_source_matches_checkout')
    require(report['success'] is True and all(v is True for v in report['checks'].values()),'laboratory_checks_passed')
    return dict(schema='sawm-independent-evidence-check/v1',verified=True,policy=report['policy'],scenario=report['scenario'],
                counts=counts,phase_metrics=phases,kernel_settings_verified=kernel_count,source_sha256=hashes,
                limits=['Checks completed laboratory evidence; not physical RF validation.',
                        'No claimed superiority over other adaptive routing algorithms.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--source-dir',type=Path)
    args=parser.parse_args()
    try:
        result=verify(args.data_dir,args.source_dir)
    except Exception as error:
        # Exception payloads from SQLite, JSON or the filesystem can contain
        # private data. Only our fixed validation labels are printed.
        result={'schema':'sawm-independent-evidence-check/v1','verified':False,
                'error':str(error) if isinstance(error,InvalidEvidence) else 'unreadable_evidence'}
    print(json.dumps(result,sort_keys=True,allow_nan=False))
    return 0 if result['verified'] else 2


if __name__=='__main__':
    raise SystemExit(main())
