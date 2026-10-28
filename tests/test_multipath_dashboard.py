"""Read-only HTTP boundary and browser rendering checks for the multipath viewer."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import multipath_dashboard as dashboard


class ViewerHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'progress.json').write_text(json.dumps({'run_id': 'fixture'}))
        (self.root / 'private.token').write_text('must-not-be-served')
        self.server = dashboard.Viewer(('127.0.0.1', 0), self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def denied(self, path, code=404, **kwargs):
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(self.url + path, **kwargs), timeout=2)
        self.assertEqual(caught.exception.code, code)
        caught.exception.close()

    def test_viewer_serves_page_with_no_store_and_security_headers(self):
        with urlopen(self.url + '/', timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b'SAWM', response.read())
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])

    def test_only_named_proof_json_is_returned(self):
        with urlopen(self.url + '/progress.json', timeout=2) as response:
            self.assertEqual(json.load(response), {'run_id': 'fixture'})
        self.denied('/private.token')

    def test_raw_and_encoded_traversal_are_rejected(self):
        self.denied('/../private.token')
        self.denied('/%2e%2e/private.token')
        self.denied('/%2fprogress.json')

    def test_symlink_proof_is_not_served(self):
        (self.root / 'report.json').symlink_to(self.root / 'private.token')
        self.denied('/report.json')

    def test_query_parameters_cannot_select_another_file(self):
        self.denied('/progress.json?path=private.token')

    def test_foreign_host_cannot_read_local_data(self):
        self.denied('/progress.json', code=403, headers={'Host': 'outside.example'})

    def test_post_does_not_change_proof(self):
        self.denied('/progress.json', code=405, data=b'{"run_id":"changed"}', method='POST')
        self.assertEqual(json.loads((self.root / 'progress.json').read_text()), {'run_id': 'fixture'})

    def test_incomplete_json_is_not_served_as_valid_proof(self):
        (self.root / 'progress.json').write_text('{unfinished')
        self.denied('/progress.json')

    def test_head_reads_metadata_without_body(self):
        with urlopen(Request(self.url + '/progress.json', method='HEAD'), timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b'')
            self.assertGreater(int(response.headers['Content-Length']), 0)


@unittest.skipUnless(shutil.which('node'), 'Node.js unavailable: Python/HTTP tests still run')
class ViewerRenderingTests(unittest.TestCase):
    """Exercise the actual browser script with a small DOM harness, no packages."""

    def test_actual_schema_renders_direction_integrity_paths_and_final_fallback(self):
        page = Path(dashboard.__file__).with_suffix('.html').read_text()
        script = page.split('<script>', 1)[1].split('</script>', 1)[0]
        script = script.replace('poll();setInterval(poll,1000);setInterval(freshness,500);', '')
        before = r'''
const assert=require('assert'),vm=require('vm');const elements=new Map();
function element(){return {textContent:'',style:{},children:[],classList:{toggle(){}},setAttribute(){},appendChild(n){this.children.push(n);return n},append(...ns){this.children.push(...ns)},replaceChildren(...ns){this.children=ns},innerHTML:''}}
const context={console,Date,Math,Number,String,Object,JSON,Promise,setInterval(){},fetch(){throw Error('unexpected network')},document:{getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id)},createElement:element,createElementNS:element,createTextNode:t=>({textContent:t})}};
'''
        render = r'''
state={run_id:'fixture',updated_at_ms:Date.now(),elapsed_ms:1000,phase:'only_d',policy:'adaptive',scenario:'standard',completed:false,success:null,nodes:{left:{generated:10,acked:8,pending:2,received:7,attempts:11,active_path:'d',controller:{state:{d:{srtt_ms:31,attempts:4,verified_acks:3}}},recent_attempts:[{path:'d',outcome:'acked',started_ms:800,ended_ms:831,selection_reason:'failed_path_bypass'}]},right:{generated:10,acked:7,pending:3,received:8,attempts:10,active_path:'d',controller:{state:{d:{srtt_ms:35,attempts:4,verified_acks:4}}},recent_attempts:[]}},phase_events:[{profile:'only_d',started_ms:500,applied_ms:510,paths:{a:{loss_pct:100,delay_ms:0},b:{loss_pct:100,delay_ms:0},c:{loss_pct:100,delay_ms:0},d:{loss_pct:0,delay_ms:15}}}]};
report={checks:{left_file_exact:true},node_results:{left:{file_exact:false,received_bytes:100,status:state.nodes.left},right:{file_exact:true,received_bytes:200,status:state.nodes.right}}};render();
if(fromReport(report).nodes.left.acked!==8)throw Error('final_status_not_mapped');
'''
        after = r'''
assert.equal(elements.get('left-acked').textContent,'8');
assert.equal(elements.get('left-received').textContent,'8');
assert.equal(elements.get('right-received').textContent,'7');
assert(elements.get('left-integrity').textContent.includes('vérifiée'));
assert(elements.get('right-integrity').textContent.includes('non conforme'));
assert(elements.get('quality-left-d').textContent.includes('3 ACK / 1 échecs'));
assert.equal(elements.get('phase').textContent,'Phase : Seul D disponible');
assert.equal(elements.get('fault-a').textContent,'Coupé par le banc');
assert.equal(elements.get('events').children.length,1);
assert(elements.get('path-chart').children.length>0);
assert(elements.get('health-label').textContent.includes('récente'));
vm.runInNewContext("state.history=[{elapsed_ms:0,left:{acked:0,pending:1},right:{acked:0,pending:1}},{elapsed_ms:1000,left:{acked:8,pending:2},right:{acked:7,pending:3}},{elapsed_ms:2000,left:{acked:-1,pending:0},right:{acked:0,pending:0}}];render();if(samples.length!==2)throw Error('history_schema_not_validated')",context);
assert(elements.get('queue-note').textContent.includes('conservées pendant le passage'));
vm.runInNewContext("state.updated_at_ms=Date.now()-5000;freshness()",context);
assert(elements.get('health-label').textContent.includes('historiques'));
vm.runInNewContext("state.completed=true;state.success=true;freshness()",context);
assert(elements.get('health-label').textContent.includes('résultats conservés'));
'''
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'viewer-test.js'
            path.write_text(before + 'vm.runInNewContext(' + json.dumps(script + render) + ',context);\n' + after)
            result = subprocess.run([shutil.which('node'), str(path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
