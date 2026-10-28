import base64
import copy
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import multipath_vm_lab as lab


class MultipathVMTests(unittest.TestCase):
    def make_queue(self,directory):
        path=Path(directory)/'node.sqlite'; lab.initialize(path); return path

    def make_snapshot(self,node):
        return {'links':[{'ifname':'lo'}]+[{'ifname':'link'+p,'addr_info':[{'family':'inet','local':lab.ADDRESSES[p][node]}]} for p in lab.PATHS],
                'routes':[{'dst':lab.ADDRESSES[p]['network'],'dev':'link'+p} for p in lab.PATHS]}

    def test_isolated_topology_rejects_default_or_extra_device(self):
        snapshot=self.make_snapshot('left')
        self.assertTrue(lab.topology_valid(snapshot,'left'))
        wrong=copy.deepcopy(snapshot); wrong['routes'].append({'dst':'default','gateway':'198.18.40.2'})
        self.assertFalse(lab.topology_valid(wrong,'left'))
        wrong=copy.deepcopy(snapshot); wrong['links'].append({'ifname':'eth0'})
        self.assertFalse(lab.topology_valid(wrong,'left'))
        wrong=copy.deepcopy(snapshot); wrong['links'][1]['addr_info'][0]['local']='127.0.0.1'
        self.assertFalse(lab.topology_valid(wrong,'left'))

    def test_node_address_space_has_four_distinct_links(self):
        self.assertEqual(len({v['network'] for v in lab.ADDRESSES.values()}),4)
        self.assertEqual(len({v[n] for v in lab.ADDRESSES.values() for n in lab.NODES}),8)
        for node in lab.NODES: self.assertTrue(lab.topology_valid(self.make_snapshot(node),node))

    def test_profile_includes_total_and_rotating_cuts(self):
        for only in ('b','c','d'):
            values=lab.profile_parameters('only_'+only)
            self.assertEqual({p for p,v in values.items() if v['loss_pct']<100},{only})
        self.assertTrue(all(v['loss_pct']==100 for v in lab.profile_parameters('all_blocked').values()))
        self.assertEqual(lab.profile_parameters('degraded_c')['c']['loss_pct'],2)
        self.assertNotEqual(lab.SCENARIOS['standard'],lab.SCENARIOS['staggered'])
        with self.assertRaises(lab.Refused): lab.profile_parameters('arbitrary')

    def test_qdisc_targets_only_owned_namespace_and_interfaces(self):
        links=lab.Links()
        for node in lab.NODES:
            for path,parameters in lab.profile_parameters('all_blocked').items():
                argv=links.qdisc_argv(node,path,parameters)
                self.assertEqual(argv[:4],['ip','netns','exec',links.names[node]])
                self.assertEqual(argv[argv.index('dev')+1],'link'+path)
                self.assertEqual(argv[-1],'100%')
                self.assertNotIn('eth0',argv)
        with self.assertRaises(lab.Refused): links.qdisc_argv('host','a',{})
        with self.assertRaises(lab.Refused): lab.ns('production','ip','link')

    def test_binary_file_chunks_reconstruct_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=self.make_queue(directory)
            for i in reversed(range(5)):
                body=lab.body_for('run','right',i,5)
                result=lab.accept_body(queue,body,'run','left',5,100+i)
                self.assertTrue(lab.verified_receipt(body,result,'left'))
            result,attempts=lab.node_result(queue,'left',5)
            self.assertTrue(result['file_exact'])
            self.assertEqual(result['received_bytes'],5*4096)
            self.assertEqual(result['received_file_sha256'],result['expected_file_sha256'])
            self.assertEqual(attempts,[])
            self.assertEqual((Path(directory)/'received-left.bin').read_bytes(),b''.join(lab.chunk('right',i) for i in range(5)))

    def test_duplicate_is_committed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=self.make_queue(directory); body=lab.body_for('run','right',0,1)
            one=lab.accept_body(queue,body,'run','left',1,10)
            two=lab.accept_body(queue,body,'run','left',1,20)
            self.assertFalse(one['duplicate']); self.assertTrue(two['duplicate'])
            with lab.connect(queue) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT received_ms FROM inbox').fetchone()[0],10)
            self.assertEqual(lab.node_status(queue)['duplicate_requests'],1)

    def test_same_identifier_different_digest_cannot_replace_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=self.make_queue(directory); body=lab.body_for('run','right',0,1)
            lab.accept_body(queue,body,'run','left',1,10)
            conflicting=dict(body,data=base64.b64encode(b'x'*4096).decode(),sha256=hashlib.sha256(b'x'*4096).hexdigest())
            with self.assertRaisesRegex(lab.Refused,'identity_content_conflict'):
                lab.accept_body(queue,conflicting,'run','left',1,20)
            self.assertEqual(lab.node_status(queue)['conflicts'],1)
            result,_=lab.node_result(queue,'left',1)
            self.assertTrue(result['file_exact'])

    def test_hash_identity_and_recipient_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=self.make_queue(directory); body=lab.body_for('run','right',0,1)
            for changed in [dict(body,sha256='0'*64),dict(body,sender='left'),dict(body,index=True),
                            dict(body,run_id='other'),dict(body,total=True),dict(body,data='bad%%%')]:
                with self.assertRaises(lab.Refused): lab.accept_body(queue,changed,'run','left',1,10)
            self.assertEqual(lab.node_status(queue)['received'],0)

    def test_receipt_requires_commit_matching_digest_and_reverse_peer(self):
        body=lab.body_for('run','left',0,1)
        valid={k:body[k] for k in ('run_id','sender','message_id','sha256')}
        valid.update(committed=True,duplicate=False,receiver='right')
        self.assertTrue(lab.verified_receipt(body,valid,'right'))
        for field,value in [('committed',1),('duplicate',1),('receiver','left'),('sha256','x'),('sender','right')]:
            self.assertFalse(lab.verified_receipt(body,dict(valid,**{field:value}),'right'))

    def test_phase_metrics_require_attempts_wholly_inside_window(self):
        phases=[{'profile':'all_blocked','applied_ms':1000,'started_ms':990},
                {'profile':'recovery','applied_ms':2000,'started_ms':1990}]
        attempt=lambda begin,end,path='a':dict(path=path,started_ms=begin,ended_ms=end,outcome='acked')
        attempts={'left':[attempt(900,1300),attempt(1250,2010),attempt(2050,2100)],
                  'right':[attempt(1200,1400,'b'),attempt(2250,2300,'c')]}
        metrics=lab.phase_metrics(attempts,phases,3000)
        self.assertEqual(metrics[0]['directions']['left']['new_acked'],0)
        self.assertEqual(metrics[0]['directions']['right']['new_acked'],1) # Would fail the outage proof.
        self.assertEqual(metrics[1]['directions']['left']['first_new_ack_ms'],100)
        self.assertEqual(metrics[1]['directions']['right']['acked_by_path']['c'],1)

    def test_fresh_effects_are_separate_from_ack_retries(self):
        phases=[{'profile':'only_d','applied_ms':1000,'started_ms':990}]
        attempts={'left':[dict(path='d',started_ms=1300,ended_ms=1400,outcome='acked')],
                  'right':[dict(path='d',started_ms=1350,ended_ms=1450,outcome='acked')]}
        results={'left':{'accepted_events':[{'received_ms':1380}]},
                 'right':{'accepted_events':[{'received_ms':800}]}}
        metrics=lab.phase_metrics(attempts,phases,2000,results)[0]['directions']
        self.assertEqual(metrics['left']['new_acked'],1)
        self.assertEqual(metrics['left']['new_effects'],0)  # Old effect ACK cannot prove new data.
        self.assertEqual(metrics['right']['new_effects'],1)
        self.assertEqual(metrics['right']['new_bytes'],4096)

    def test_two_payload_streams_are_different(self):
        self.assertNotEqual(lab.chunk('left',0),lab.chunk('right',0))
        self.assertNotEqual(lab.chunk('left',0),lab.chunk('left',1))
        self.assertEqual(len(lab.chunk('left',0)),4096)

    def test_cleanup_attempts_every_namespace_after_error(self):
        links=lab.Links(); links.created_ns=list(links.names.values()); links.created_host=['mltest']
        calls=[]
        def call(argv,check=True):
            calls.append(argv)
            if argv==['ip','netns','delete',links.names['right']]: raise TimeoutError()
            return types.SimpleNamespace(returncode=0,stdout='[]',stderr='')
        links.call=call
        result=links.close()
        self.assertFalse(result[links.names['right']])
        self.assertTrue(result[links.names['left']])
        self.assertIn(['ip','netns','delete',links.names['left']],calls)
        self.assertTrue(result['mltest'])

    def test_private_token_and_atomic_output(self):
        with tempfile.TemporaryDirectory() as directory:
            token=Path(directory)/'token'; token.write_text('a'*32); token.chmod(0o600)
            self.assertEqual(lab.token_from(token),'a'*32)
            token.chmod(0o644)
            with self.assertRaises(lab.Refused): lab.token_from(token)
            output=Path(directory)/'progress.json'; lab.save(output,{'n':1}); lab.save(output,{'n':2})
            self.assertEqual(json.loads(output.read_text()),{'n':2})
            self.assertEqual(list(Path(directory).glob('*.tmp-*')),[])

    def test_endpoint_real_http_unauthenticated_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            queue=self.make_queue(directory)
            server=lab.Receiver(('127.0.0.1',0),lab.ReceiverHandler)
            server.allowed_sources={'127.0.0.1'}; server.queue=queue; server.node='left'
            server.run_id='run'; server.count=1; server.token='a'*32; server.start_ns=time.monotonic_ns()
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True); thread.start()
            try:
                body=lab.body_for('run','right',0,1)
                for authorized,code in [(False,409),(True,200)]:
                    connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
                    headers={'Content-Type':'application/json'}
                    if authorized: headers['Authorization']='Bearer '+server.token
                    connection.request('POST','/chunk',lab.canonical(body),headers)
                    response=connection.getresponse(); receipt=json.loads(response.read()); connection.close()
                    self.assertEqual(response.status,code)
                    if authorized: self.assertTrue(lab.verified_receipt(body,receipt,'left'))
                self.assertEqual(lab.node_status(queue)['received'],1)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_cli_requires_explicit_run_before_mutation(self):
        result=subprocess.run([sys.executable,str(Path(lab.__file__)),'--token-file','missing'],capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('explicit_run_and_output_required',result.stderr)


if __name__=='__main__': unittest.main()
