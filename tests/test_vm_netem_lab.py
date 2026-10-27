"""VM-lab contracts and honest analysis; no root/network mutations in unit tests."""
import contextlib
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import vm_netem_lab as lab
import delivery
import witness


class VmNetemTests(unittest.TestCase):
    def test_only_predefined_profiles_and_owned_namespace_names_are_accepted(self):
        name='sawm-nem-123456789abc-tx'
        for phase in lab.PHASES:
            args=lab.qdisc_command(name,phase)
            self.assertEqual(args[:4],['ip','netns','exec',name])
            self.assertEqual(args[4:11],['tc','qdisc','replace','dev','link0','root','handle'])
            self.assertNotIn('eth0',args)
            self.assertNotIn('seed',args)
        for bad in ('default','../netns','sawm-nem-123456789abc-rx;id','sawm-nem-123456789abc-mgmt',None):
            with self.subTest(bad=bad), self.assertRaises(delivery.Refused):lab.qdisc_command(bad,lab.PHASES[0])
        with self.assertRaises(delivery.Refused):lab.qdisc_command(name,{**lab.PHASES[0],'loss_percent':42})
        self.assertIn('100%',lab.qdisc_command(name,lab.PHASES[2]))

    def test_namespace_scope_rejects_default_route_wrong_address_and_extra_link(self):
        sample={'links':[{'ifname':'lo','addr_info':[]},{'ifname':'link0','addr_info':[{'family':'inet','local':lab.SOURCE}]}],
                'routes':[{'dst':'198.18.0.0/30','dev':'link0'}]}
        self.assertTrue(lab.isolated_topology(sample,'tx'))
        self.assertFalse(lab.isolated_topology(sample,'rx'))
        for mutate in (lambda s:s['routes'].append({'dst':'default','gateway':'198.18.0.2','dev':'link0'}),
                       lambda s:s['links'].append({'ifname':'eth0'}),
                       lambda s:s['routes'][0].update(gateway='198.18.0.2')):
            s=copy.deepcopy(sample);mutate(s);self.assertFalse(lab.isolated_topology(s,'tx'))

    def test_destination_bootstrap_is_scoped_even_on_error(self):
        original=delivery.HOST
        with self.assertRaises(RuntimeError):
            with lab.lab_destination():
                self.assertEqual(delivery.HOST,'198.18.0.2')
                raise RuntimeError('test')
        self.assertEqual(delivery.HOST,original)

    def test_messages_are_paired_but_repetitions_are_distinct(self):
        self.assertEqual(lab.messages_for(1),lab.messages_for(1))
        self.assertNotEqual(lab.messages_for(1),lab.messages_for(2))
        for message in lab.messages_for(1): delivery.validate_message(message)
        for bad in (0,4,True,1.5):
            with self.assertRaises(delivery.Refused):lab.messages_for(bad)

    def test_phase_analysis_preserves_transition_window(self):
        phases=[{'profile':{'name':'healthy'},'started_monotonic_ns':100,'completed_monotonic_ns':120},
                {'profile':{'name':'degraded'},'started_monotonic_ns':200,'completed_monotonic_ns':250}]
        self.assertEqual(lab.phase_at(99,phases),'setup')
        self.assertEqual(lab.phase_at(110,phases),'transition')
        self.assertEqual(lab.phase_at(199,phases),'healthy')
        self.assertEqual(lab.phase_at(210,phases),'transition')
        self.assertEqual(lab.phase_at(250,phases),'degraded')

    def test_partial_setup_cleanup_never_deletes_unowned_namespace(self):
        link=lab.Link(); link.created=[link.names['tx']]
        calls=[]
        def command(args,check=True):
            calls.append(args)
            return Mock(returncode=0,stdout='other-existing\n',stderr='')
        with patch.object(lab,'command',command): self.assertTrue(link.close())
        deletes=[a for a in calls if a[:3]==['ip','netns','delete']]
        self.assertEqual(deletes,[['ip','netns','delete',link.names['tx']]])
        self.assertFalse(any('other-existing' in a for a in calls))

    def test_cleanup_attempts_all_children_and_link_even_when_wait_raises(self):
        worker, server, link=object(),object(),Mock()
        link.close.return_value=True
        with patch.object(lab,'stop_owned',side_effect=[TimeoutError('worker'),True]) as stop:
            result=lab.cleanup_owned(worker,server,link)
        self.assertEqual(stop.call_args_list,[unittest.mock.call(worker),unittest.mock.call(server)])
        self.assertFalse(result['worker']['stopped'])
        self.assertTrue(result['witness']['stopped'])
        self.assertTrue(result['link']['stopped'])
        link.close.assert_called_once()

    def test_namespace_cleanup_continues_after_command_timeout(self):
        link=lab.Link(); link.created=list(link.names.values());calls=[]
        def command(args,check=True):
            calls.append(args)
            if args==['ip','netns','delete',link.names['rx']]:raise TimeoutError('command')
            return Mock(returncode=0,stdout='',stderr='')
        with patch.object(lab,'command',command):self.assertFalse(link.close())
        self.assertIn(['ip','netns','delete',link.names['tx']],calls)
        self.assertIn(['ip','netns','list'],calls)

    def test_analysis_keeps_missing_ack_distinct_from_delivery_and_missing_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);queue=root/'queue.sqlite';ledger=root/'witness.sqlite'
            planned=lab.messages_for(1);witness.initialize(ledger)
            arrivals=[]
            with lab.lab_destination():
                delivery.create_queue(queue,lab.PORT,20)
                for message in planned:
                    now=time.monotonic_ns();delivery.enqueue(queue,message)
                    arrivals.append({'message_id':message['message_id'],'enqueue_started_monotonic_ns':now})
            witness.commit_message(ledger,'durable',planned[0],delivery.digest(planned[0]),False)
            phases=[{'profile':{'name':p['name']},'started_monotonic_ns':1+i*1000,'completed_monotonic_ns':2+i*1000} for i,p in enumerate(lab.PHASES)]
            result=lab.analyze(queue,ledger,arrivals,phases,planned)
            self.assertEqual(result['metrics']['delivered_unique'],1)
            self.assertEqual(result['metrics']['acked'],0)
            self.assertFalse(result['metrics']['all_messages_delivered_and_acked'])
            self.assertIsNone(result['metrics']['ack_p95_ms'])
            self.assertIsNone(result['metrics']['first_ack_after_recovery_ms'])
            self.assertFalse(result['checks']['worker_known_outcome'])
            self.assertTrue(result['checks']['received_content_matches_source'])

    def test_linux_root_is_required_before_any_network_call(self):
        with patch.object(lab.sys,'platform','darwin'),patch.object(lab,'command') as call:
            with self.assertRaises(delivery.Refused):lab.run(Path('/unused-test-path'))
            call.assert_not_called()


if __name__=='__main__':unittest.main()
