import copy
import unittest
from unittest.mock import Mock, patch
from concurrent.futures import Future

from kasciidash_app.ui import dashboard as ui
from kasciidash_app.resources import diagnostics as dg
from kasciidash_app.resources import resource_detail as rd


def obj(kind, name, uid, owner=None, **extra):
    value = dict(kind=kind, metadata=dict(name=name, namespace='test', uid=uid, generation=1), **extra)
    if owner:
        value['metadata']['ownerReferences'] = [dict(uid=owner['metadata']['uid'], kind=owner['kind'], name=owner['metadata']['name'], controller=True)]
    return value


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.dep = obj('Deployment', 'web', 'd', spec={'replicas': 2}, status={'observedGeneration': 1, 'updatedReplicas': 1, 'readyReplicas': 1, 'availableReplicas': 1, 'replicas': 2})
        self.rs = obj('ReplicaSet', 'web-rs', 'rs', self.dep)
        self.pod = obj('Pod', 'web-pod', 'p', self.rs, spec={}, status={'phase': 'Pending', 'conditions': [{'type': 'PodScheduled', 'status': 'False', 'reason': 'Unschedulable', 'message': 'SECRET'}], 'containerStatuses': [{'name': 'app', 'restartCount': 7, 'state': {'waiting': {'reason': 'CrashLoopBackOff', 'message': 'SECRET'}}, 'lastState': {'terminated': {'reason': 'OOMKilled', 'exitCode': 137}}}]})
        self.objects = {'replicasets.apps': [self.rs], 'pods': [self.pod], 'events': [], 'resourcequotas': [], 'persistentvolumeclaims': []}
        self.item = dict(resource='deployments.apps', name='web', namespace='test', namespaced=True, uid='d')

    def report(self):
        return dg.report(self.dep, self.objects, ['pods: collected'], 'fixture UTC')

    def test_evidence_and_conservative_explanations(self):
        report = self.report()
        for text in ('rollout incomplete', 'updatedReplicas=1', 'PodScheduled=False', 'CrashLoopBackOff', 'cumulative restartCount=7', 'not a recent-time rate', 'does not establish a memory leak', 'fixture UTC', 'Next check:', 'initContainerStatuses: uncollected'):
            self.assertIn(text, report)
        self.assertNotIn('SECRET', report)

    def test_missing_and_stale_deployment_fields_are_not_zero(self):
        self.dep['status'] = {'observedGeneration': 1}
        self.assertIn('completion unknown', self.report())
        self.assertIn('readyReplicas=uncollected', self.report())
        self.dep['status']['observedGeneration'] = 0
        self.assertIn('status stale', self.report())
        self.assertNotIn('rollout incomplete', self.report())

    def test_owner_uid_controller_and_namespace_isolation(self):
        neighbour = copy.deepcopy(self.pod)
        neighbour['metadata']['ownerReferences'][0]['uid'] = 'different'
        neighbour['metadata']['name'] = 'neighbour'
        self.objects['pods'].append(neighbour)
        self.assertNotIn('neighbour', self.report())
        self.rs['metadata']['ownerReferences'][0]['controller'] = False
        self.assertNotIn('CrashLoopBackOff', self.report())

    def test_job_statefulset_do_not_get_deployment_rules(self):
        for kind in ('Job', 'StatefulSet'):
            self.dep['kind'] = kind
            self.assertNotIn('Deployment rollout', self.report())
            self.assertFalse(dg.supports(dict(resource=kind.lower()+'s')))

    def test_events_scoped_by_uid_and_messages_withheld(self):
        self.objects['events'] = [{'involvedObject': {'uid': 'rs'}, 'reason': 'FailedCreate', 'message': 'SECRET', 'lastTimestamp': 'old', 'count': 3}, {'involvedObject': {'uid': 'other'}, 'reason': 'FailedMount'}]
        report = self.report()
        self.assertIn('FailedCreate; time=old; count=3', report)
        self.assertNotIn('FailedMount', report)
        self.assertNotIn('SECRET', report)

    def test_pvc_quota_are_evidence_not_cause(self):
        self.pod['spec']['volumes'] = [{'persistentVolumeClaim': {'claimName': 'data'}}]
        self.objects['persistentvolumeclaims'] = [obj('PersistentVolumeClaim', 'data', 'c', status={'phase': 'Pending'})]
        self.objects['resourcequotas'] = [obj('ResourceQuota', 'quota', 'q', status={'hard': {'pods': '10'}, 'used': {'pods': '10'}})]
        report = self.report()
        self.assertIn('Referenced PVC data: phase=Pending', report)
        self.assertIn('not proof of this incident', report)

    def client(self, denied=False, replacement=False):
        def call(*args):
            if args[2] == 'web':
                value = copy.deepcopy(self.dep)
                if replacement:
                    value['metadata']['uid'] = 'replacement'
                return value, None
            resource = args[1]
            if denied and resource == 'pods':
                return None, 'Forbidden SECRET'
            return {'items': self.objects[resource]}, None
        client = Mock()
        client.call.side_effect = call
        return client

    def test_only_gets_no_discovery_secret_or_logs_and_partial_denial(self):
        client = self.client(denied=True)
        output, error = rd.load(client, self.item, 'diagnostics')
        self.assertIsNone(error)
        self.assertIn('pods: unavailable', output)
        self.assertIn('Pod relationship evidence incomplete', output)
        self.assertNotIn('SECRET', output)
        for call in client.call.call_args_list:
            self.assertEqual(call.args[0], 'get')
            self.assertNotIn('secrets', call.args)
            self.assertIn('--namespace', call.args)
        client.text.assert_not_called()

    def test_replaced_target_does_not_collect_or_publish(self):
        client = self.client(replacement=True)
        self.assertIn('identity changed', dg.load(client, self.item)[0])
        self.assertEqual(client.call.call_count, 1)
        client = self.client()
        original = client.call.side_effect
        calls = []
        def changing(*args):
            calls.append(args)
            value, error = original(*args)
            if len(calls) == 7:
                value['metadata']['generation'] = 2
            return value, error
        client.call.side_effect = changing
        self.assertIn('changed during collection', dg.load(client, self.item)[0])

    def test_tabs_preserved_demo_and_invalid_identity(self):
        self.assertEqual(rd.detail_modes(self.item), ['yaml', 'describe', 'logs', 'topology', 'warnings', 'errors', 'diagnostics'])
        self.assertIn('DEMO', rd.demo(self.item, 'diagnostics')[0])
        client = Mock()
        self.assertIn('Namespace', rd.load(client, dict(self.item, namespace='bad;name'), 'diagnostics')[1])
        client.call.assert_not_called()

    def test_inflight_diagnostic_result_discarded_after_switch(self):
        queue = []
        class Pool:
            def submit(self, *args):
                future = Future(); future.set_running_or_notify_cancel(); queue.append(future); return future
        detail = rd.Detail(); detail.open(self.item, 0); detail.mode = 'diagnostics'
        detail.poll(Pool(), lambda *args: None)
        detail.close(); detail.open(dict(self.item, name='other', uid='other'), 0)
        queue[0].set_result(('OLD DIAGNOSTICS', None))
        detail.poll(Pool(), lambda *args: None)
        self.assertNotIn('OLD DIAGNOSTICS', str(detail.state()))


    def test_pod_status_uses_final_verified_get(self):
        first = obj('Pod', 'web', 'p', spec={}, status={'phase': 'Running', 'containerStatuses': [{'name': 'app', 'restartCount': 0, 'state': {'running': {}}}]})
        current = copy.deepcopy(first)
        current['metadata']['resourceVersion'] = '2'
        current['status'] = {'phase': 'Failed', 'containerStatuses': [{'name': 'app', 'restartCount': 1, 'state': {'terminated': {'reason': 'OOMKilled', 'exitCode': 137}}}]}
        client = Mock()
        client.call.side_effect = [(first, None)] + [({'items': []}, None)] * 3 + [(current, None)]
        output, error = rd.load(client, dict(resource='pods', name='web', namespace='test', namespaced=True, uid='p'), 'diagnostics')
        self.assertIsNone(error)
        self.assertIn('phase=Failed', output)
        self.assertIn('cumulative restartCount=1', output)
        self.assertIn('current OOM termination observed', output)
        self.assertNotIn('phase=Running', output)
        self.assertNotIn('cumulative restartCount=0', output)

    def test_current_and_previous_terminations_are_independent(self):
        container = self.pod['status']['containerStatuses'][0]
        for current_reason, previous_reason in [('OOMKilled', 'Error'), ('Error', 'OOMKilled'), ('OOMKilled', 'OOMKilled')]:
            with self.subTest(current=current_reason, previous=previous_reason):
                container['state'] = {'terminated': {'reason': current_reason, 'exitCode': 137, 'finishedAt': 'current-time'}}
                container['lastState'] = {'terminated': {'reason': previous_reason, 'exitCode': 1, 'finishedAt': 'previous-time'}}
                output = self.report()
                self.assertIn('current termination reason=' + current_reason, output)
                self.assertIn('previous termination reason=' + previous_reason, output)
                self.assertIn('finishedAt=current-time; source=state.terminated', output)
                self.assertIn('finishedAt=previous-time; source=lastState.terminated', output)
                for label, reason in [('current', current_reason), ('previous', previous_reason)]:
                    self.assertEqual(label + ' OOM termination observed' in output, reason == 'OOMKilled')

    def test_diagnostics_keyboard_resize_refresh_and_read_only_guard(self):
        class Pool:
            def __init__(self,**kw):pass
            def submit(self,fn,*args):
                future=Future();future.set_result(fn(*args));return future
            def shutdown(self,**kw):pass
        keys=[ord('3'),10,10,ui.curses.KEY_LEFT,-1,*map(ord,'neDa!'),ord('d'),ord('u'),ui.curses.KEY_RESIZE,ord('r'),-1,ui.curses.KEY_RIGHT,ord('e'),27,-1,ord('q')]
        class Terminal:
            def __init__(self):self.index=0;self.frames=[];self.current=[]
            def timeout(self,v):pass
            def getmaxyx(self):return [(45,160),(18,60),(24,84),(30,100)][(self.index//5)%4]
            def erase(self):self.current=[]
            def addnstr(self,y,x,s,n,*a):self.current.append((y,s[:n]))
            def refresh(self):self.frames.append(list(self.current))
            def getch(self):
                key=keys[self.index];self.index+=1;return key
        terminal=Terminal();actions=[];loads=[]
        def load(item,mode):loads.append(mode);return rd.demo(item,mode)
        with patch.object(ui.concurrent.futures,'ThreadPoolExecutor',Pool),patch.object(ui.curses,'curs_set'),patch.object(ui.curses,'set_escdelay',create=True),patch.object(ui.curses,'mousemask'),patch.object(ui.curses,'mouseinterval'),patch.object(ui.sys,'stdout'),patch.object(ui,'terminal_action',side_effect=lambda screen,callback:callback()):
            self.assertEqual(ui.screen(terminal,ui.demo,30,detail_loader=load,action_handler=lambda a,i:actions.append(a)),'quit')
        self.assertEqual(actions,['edit'])
        self.assertEqual(loads.count('diagnostics'),2)
        self.assertNotIn('logs',loads)
        footers = [text for frame in terminal.frames for _, text in frame if text.startswith('DIAGNOSTICS:')]
        self.assertTrue(footers)
        self.assertTrue(any('READ ONLY' in text and 'r reload' in text for text in footers))
        for footer in footers:
            for blocked in ('e edit', 'n new', 'D drafts', 'a actions', '! exec'):
                self.assertNotIn(blocked, footer)


if __name__ == '__main__':
    unittest.main()
