#!/usr/bin/env python3
"""Four-path, bidirectional IP resilience laboratory in two isolated Linux namespaces.

Real TCP/HTTP packets and synthetic file chunks; network impairment uses netem.
No RF model, radios, Cloud decision calls, or production-network modification.
The endpoint processes see completed transport observations, never the scenario.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid

from multipath_policy import MultipathSelector

PATHS = ('a', 'b', 'c', 'd')
NODES = ('left', 'right')
PORT = 18068
CHUNK_BYTES = 4096
MAX_ATTEMPTS = 80
BUDGET_SECONDS = 105
COUNT = 220
INTERVAL_MS = 250
NAMESPACE = re.compile(r'sawm-mp-[a-f0-9]{10}-(left|right)\Z')
ADDRESSES = {p: {'left': f'198.18.{40+i}.1', 'right': f'198.18.{40+i}.2',
                 'network': f'198.18.{40+i}.0/30'} for i,p in enumerate(PATHS)}
SCENARIOS = {
    'standard': ((0,'nominal'),(6000,'cut_a'),(12000,'cut_ab'),(18000,'only_d'),
                 (24000,'only_b'),(30000,'only_c'),(36000,'degraded_c'),
                 (42000,'all_blocked'),(47000,'recovery')),
    'staggered': ((0,'nominal'),(6000,'only_c'),(12000,'only_b'),(18000,'only_d'),
                  (24000,'cut_ab'),(30000,'only_c'),(36000,'degraded_c'),
                  (42000,'all_blocked'),(47000,'recovery')),
}
PROFILES = frozenset(p for timeline in SCENARIOS.values() for _,p in timeline)


class Refused(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def save(path,value):
    path=Path(path)
    temporary=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    temporary.write_text(json.dumps(value,indent=2,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    os.replace(temporary,path)


def elapsed(start):
    return (time.monotonic_ns()-start)/1e6


def token_from(path):
    path=Path(path); info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode)==0o600,'private_regular_token_required')
    require(info.st_size<=4096,'token_too_large')
    value=path.read_text(encoding='ascii').strip()
    require(re.fullmatch(r'[A-Za-z0-9_.~-]{16,512}',value) is not None,'invalid_token')
    return value


def command(argv,check=True):
    result=subprocess.run(argv,capture_output=True,text=True,timeout=5)
    if check and result.returncode:
        raise Refused('command_failed:'+Path(argv[0]).name+':'+result.stderr[:180])
    return result


def ns(name,*argv):
    require(bool(NAMESPACE.fullmatch(name)),'unowned_namespace')
    return ['ip','netns','exec',name,*argv]


def snapshot(name=None):
    prefix=lambda *a: ns(name,*a) if name else list(a)
    return {'links':json.loads(command(prefix('ip','-j','address','show')).stdout),
            'routes':json.loads(command(prefix('ip','-j','route','show','table','main')).stdout)}


def primary_signature():
    raw=snapshot()
    return {'links':{r['ifname']:{'mtu':r.get('mtu'),'address':r.get('address'),
              'addresses':sorted((a.get('family'),a.get('local'),a.get('prefixlen')) for a in r.get('addr_info',[]))}
              for r in raw['links']},'routes':raw['routes']}


def topology_valid(raw,node):
    if node not in NODES or {r['ifname'] for r in raw['links']}!={'lo',*('link'+p for p in PATHS)}:
        return False
    for p in PATHS:
        row=next(r for r in raw['links'] if r['ifname']=='link'+p)
        ips=[a.get('local') for a in row.get('addr_info',[]) if a.get('family')=='inet']
        if ips!=[ADDRESSES[p][node]]: return False
    return len(raw['routes'])==4 and all(any(r.get('dst')==ADDRESSES[p]['network'] and
        r.get('dev')=='link'+p and 'gateway' not in r for r in raw['routes']) for p in PATHS)


def profile_parameters(profile):
    require(profile in PROFILES,'unknown_profile')
    blocked=set()
    if profile=='cut_a': blocked={'a'}
    elif profile=='cut_ab': blocked={'a','b'}
    elif profile.startswith('only_'): blocked=set(PATHS)-{profile[-1]}
    elif profile=='degraded_c': blocked={'a','b','d'}
    elif profile=='all_blocked': blocked=set(PATHS)
    return {p:{'delay_ms':(35 if profile=='degraded_c' and p=='c' else 3+PATHS.index(p)*3),
               'jitter_ms':(5 if profile=='degraded_c' and p=='c' else 0),
               'loss_pct':100 if p in blocked else (2 if profile=='degraded_c' and p=='c' else 0)} for p in PATHS}


class Links:
    def __init__(self):
        suffix=uuid.uuid4().hex[:10]
        self.names={n:f'sawm-mp-{suffix}-{n}' for n in NODES}
        self.host_names={p:(f'ml{suffix}{p}',f'mr{suffix}{p}') for p in PATHS}
        self.created_ns=[]; self.created_host=[]; self.log=[]

    def call(self,argv,check=True):
        result=command(argv,check)
        self.log.append({'argv':argv,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
        return result

    def create(self):
        existing={line.split()[0] for line in command(['ip','netns','list']).stdout.splitlines() if line.strip()}
        require(not existing.intersection(self.names.values()),'namespace_collision')
        interfaces={r['ifname'] for r in snapshot()['links']}
        require(not interfaces.intersection(x for pair in self.host_names.values() for x in pair),'interface_collision')
        for name in self.names.values():
            self.created_ns.append(name)
            self.call(['ip','netns','add',name])
        for p in PATHS:
            a,b=self.host_names[p]
            self.created_host.append(a)
            self.call(['ip','link','add',a,'type','veth','peer','name',b])
            for node,interface in zip(NODES,(a,b)):
                name=self.names[node]
                self.call(['ip','link','set',interface,'netns',name])
                self.call(ns(name,'ip','link','set',interface,'name','link'+p))
                self.call(ns(name,'ip','address','add',ADDRESSES[p][node]+'/30','dev','link'+p))
                self.call(ns(name,'ip','link','set','link'+p,'up'))
        for node,name in self.names.items():
            self.call(ns(name,'ip','link','set','lo','up'))
            require(topology_valid(snapshot(name),node),'invalid_namespace_topology')
        return {n:snapshot(name) for n,name in self.names.items()}

    def qdisc_argv(self,node,path,parameters):
        require(node in NODES and path in PATHS,'qdisc_outside_lab')
        args=ns(self.names[node],'tc','qdisc','replace','dev','link'+path,'root','handle','10:',
                'netem','limit','256','delay',str(parameters['delay_ms'])+'ms')
        if parameters['jitter_ms']: args.append(str(parameters['jitter_ms'])+'ms')
        return args+['loss','random',str(parameters['loss_pct'])+'%']

    def apply(self,profile,start):
        parameters=profile_parameters(profile)
        event={'profile':profile,'started_ms':elapsed(start),'paths':parameters}
        for node in NODES:
            for path in PATHS:
                self.call(self.qdisc_argv(node,path,parameters[path]))
        event['applied_ms']=elapsed(start)
        readback={}
        for node in NODES:
            for path in PATHS:
                raw=self.call(ns(self.names[node],'tc','-s','-j','qdisc','show','dev','link'+path)).stdout
                rows=json.loads(raw)
                require(any(r.get('kind')=='netem' for r in rows),'netem_readback_missing')
                readback[node+'_'+path]=rows
        # Keep kernel counters/options in the independent diagnostic file.
        self.log.append({'phase':profile,'readback':readback,'readback_ms':elapsed(start)})
        return event

    def close(self):
        results={}
        for name in reversed(self.created_ns):
            try:
                results[name]=self.call(['ip','netns','delete',name],False).returncode==0
            except Exception: results[name]=False
        for name in self.created_host:
            try:
                rows=json.loads(self.call(['ip','-j','link','show']).stdout)
                if name in {r['ifname'] for r in rows}:
                    self.call(['ip','link','delete',name],False)
                results[name]=name not in {r['ifname'] for r in json.loads(self.call(['ip','-j','link','show']).stdout)}
            except Exception: results[name]=False
        return results


@contextmanager
def connect(path):
    db=sqlite3.connect(path,timeout=3); db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=3000')
    try:
        with db: yield db
    finally: db.close()


def initialize(path):
    with connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE outbox (idx INTEGER PRIMARY KEY, body TEXT NOT NULL, enqueued_ms REAL NOT NULL,
          ack_ms REAL, attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE inbox (idx INTEGER PRIMARY KEY, message_id TEXT UNIQUE NOT NULL, sha256 TEXT NOT NULL,
          data BLOB NOT NULL, received_ms REAL NOT NULL);
        CREATE TABLE attempts (id INTEGER PRIMARY KEY, message_id TEXT, path TEXT, started_ms REAL,
          ended_ms REAL,outcome TEXT,selection_reason TEXT,receipt TEXT);
        CREATE TABLE state (key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        for key in ('duplicate_requests','conflicts','rejected'):
            db.execute('INSERT INTO state VALUES (?,?)',(key,'0'))


def put_state(db,key,value):
    db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,canonical(value)))


def get_state(db,key,default=None):
    row=db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default


def chunk(node,index):
    require(node in NODES and type(index) is int and 0<=index<1000,'chunk_parameters')
    seed=hashlib.sha256(f'SAWM synthetic file / {node} / {index}'.encode()).digest()
    return (seed*(CHUNK_BYTES//len(seed)+1))[:CHUNK_BYTES]


def body_for(run_id,node,index,count):
    data=chunk(node,index)
    return {'run_id':run_id,'sender':node,'message_id':f'{node}-{index:04d}',
            'index':index,'total':count,'data':base64.b64encode(data).decode(),
            'sha256':hashlib.sha256(data).hexdigest()}


def accept_body(queue,body,run_id,receiver,count,now):
    other='right' if receiver=='left' else 'left'
    require(type(body) is dict and set(body)=={'run_id','sender','message_id','index','total','data','sha256'},'body_schema')
    index=body.get('index')
    require(body['run_id']==run_id and body['sender']==other and type(index) is int and 0<=index<count and
            body['total']==count and type(body['total']) is int and body['message_id']==f'{other}-{index:04d}', 'body_identity')
    require(type(body['data']) is str and len(body['data'])<=6000,'data_size')
    try: data=base64.b64decode(body['data'],validate=True)
    except Exception: raise Refused('data_encoding')
    require(len(data)==CHUNK_BYTES and type(body['sha256']) is str and
            hashlib.sha256(data).hexdigest()==body['sha256'],'data_digest')
    with connect(queue) as db:
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT * FROM inbox WHERE idx=?',(index,)).fetchone()
        if previous is not None:
            if previous['sha256']!=body['sha256'] or bytes(previous['data'])!=data:
                put_state(db,'conflicts',get_state(db,'conflicts',0)+1)
                # Commit the refusal counter before raising outside the transaction.
                conflict=True
            else:
                put_state(db,'duplicate_requests',get_state(db,'duplicate_requests',0)+1); conflict=False
        else:
            db.execute('INSERT INTO inbox VALUES (?,?,?,?,?)',(index,body['message_id'],body['sha256'],data,now))
            conflict=False
    require(not conflict,'identity_content_conflict')
    return {'run_id':run_id,'sender':other,'receiver':receiver,'message_id':body['message_id'],
            'sha256':body['sha256'],'committed':True,'duplicate':previous is not None}


def verified_receipt(body,receipt,receiver):
    return (type(receipt) is dict and receipt.get('committed') is True and
            type(receipt.get('duplicate')) is bool and receipt.get('receiver')==receiver and
            all(receipt.get(k)==body[k] for k in ('run_id','sender','message_id','sha256')))


class Receiver(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=False
    # Avoid DNS on the isolated addresses.
    def server_bind(self):
        self.socket.bind(self.server_address)
        self.server_address=self.socket.getsockname()
        self.server_name=self.server_address[0]; self.server_port=self.server_address[1]


class ReceiverHandler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        try:
            self.connection.settimeout(2)
            require(self.path=='/chunk','route')
            require(self.client_address[0] in self.server.allowed_sources,'peer_address')
            require(hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+self.server.token),'authentication')
            require(not self.headers.get('Transfer-Encoding'),'chunked_request')
            size=int(self.headers.get('Content-Length','0'))
            require(0<size<=8192,'request_size')
            data=self.rfile.read(size); require(len(data)==size,'short_body')
            body=json.loads(data)
            require(self.server.start_ns is not None,'not_started')
            reply=accept_body(self.server.queue,body,self.server.run_id,self.server.node,
                              self.server.count,elapsed(self.server.start_ns))
            encoded=canonical(reply).encode()
            self.send_response(200)
        except Exception:
            encoded=b'{"error":"request_refused"}'
            self.send_response(409)
        try:
            self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(encoded)))
            self.end_headers(); self.wfile.write(encoded)
        except (BrokenPipeError,ConnectionResetError): pass


def request(path,node,body,token,timeout_ms):
    other='right' if node=='left' else 'left'
    connection=http.client.HTTPConnection(ADDRESSES[path][other],PORT,timeout=timeout_ms/1000)
    try:
        connection.request('POST','/chunk',canonical(body),{'Content-Type':'application/json','Authorization':'Bearer '+token})
        response=connection.getresponse(); raw=response.read(2049)
        require(response.status==200 and len(raw)<=2048,'http_receipt_refused')
        result=json.loads(raw)
        require(verified_receipt(body,result,other),'invalid_receipt')
        return result
    finally: connection.close()


def node_status(queue):
    with connect(queue) as db:
        row=db.execute('SELECT count(*),sum(ack_ms IS NOT NULL),sum(attempts) FROM outbox').fetchone()
        generated,acked,attempts=(x or 0 for x in row)
        controller=get_state(db,'controller',{})
        recent=[dict(r) for r in db.execute('SELECT message_id,path,started_ms,ended_ms,outcome,selection_reason FROM attempts ORDER BY id DESC LIMIT 20')][::-1]
        return {'generated':generated,'acked':acked,'pending':generated-acked,
                'attempts':attempts,'received':db.execute('SELECT count(*) FROM inbox').fetchone()[0],
                'active_path':get_state(db,'active_path','a'),'controller':controller,'recent_attempts':recent,
                'duplicate_requests':get_state(db,'duplicate_requests',0),'conflicts':get_state(db,'conflicts',0),
                'sending_done':get_state(db,'sending_done',False)}


def node_run(node,namespace,queue,start_file,token_file,run_id,count,interval_ms,policy,budget):
    require(node in NODES and bool(NAMESPACE.fullmatch(namespace)),'node_arguments')
    require(os.stat('/proc/self/ns/net').st_ino==os.stat('/var/run/netns/'+namespace).st_ino,'node_not_isolated')
    require(topology_valid(snapshot(),node),'node_topology_invalid')
    token=token_from(token_file)
    selector=MultipathSelector(paths=PATHS)
    server=Receiver(('0.0.0.0',PORT),ReceiverHandler)
    server.allowed_sources={ADDRESSES[p]['right' if node=='left' else 'left'] for p in PATHS}
    server.queue=queue; server.node=node; server.run_id=run_id; server.count=count; server.token=token; server.start_ns=None
    receiver_thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True)
    receiver_thread.start()
    Path(str(queue)+'.ready').write_text('ready')
    try:
        wait_start=time.monotonic()
        while not Path(start_file).exists():
            require(time.monotonic()-wait_start<15,'start_barrier_timeout'); time.sleep(.01)
        start=json.loads(Path(start_file).read_text())['start_ns']; server.start_ns=start
        generated=0
        while elapsed(start)<budget*1000:
            now=elapsed(start)
            with connect(queue) as db:
                while generated<count and now>=generated*interval_ms:
                    body=body_for(run_id,node,generated,count)
                    db.execute('INSERT INTO outbox(idx,body,enqueued_ms) VALUES (?,?,?)',(generated,canonical(body),now)); generated+=1
                row=db.execute('SELECT * FROM outbox WHERE ack_ms IS NULL AND attempts<? ORDER BY attempts,idx LIMIT 1',(MAX_ATTEMPTS,)).fetchone()
                pending=db.execute('SELECT count(*) FROM outbox WHERE ack_ms IS NULL').fetchone()[0]
                if generated==count and pending==0:
                    put_state(db,'sending_done',True)
            if row is None:
                time.sleep(.02); continue  # receiver must remain alive for the peer.
            wait=selector.retry_delay_ms(now) if policy=='adaptive' else 0
            if wait>0:
                time.sleep(min(.2,wait/1000)); continue
            path=selector.select(now) if policy=='adaptive' else 'a'
            reason=selector.last_decision.get('reason','observation_policy') if policy=='adaptive' else 'fixed_a_reference'
            timeout=selector.request_timeout_ms(path) if policy=='adaptive' else 350
            body=json.loads(row['body']); receipt=None; began=elapsed(start)
            try:
                receipt=request(path,node,body,token,timeout); outcome='acked'
            except TimeoutError: outcome='timeout'
            except Exception as exc: outcome='transport_error:'+type(exc).__name__
            ended=elapsed(start)
            if policy=='adaptive': selector.observe(path,outcome=='acked',ended-began,ended)
            with connect(queue) as db:
                db.execute('INSERT INTO attempts(message_id,path,started_ms,ended_ms,outcome,selection_reason,receipt) VALUES (?,?,?,?,?,?,?)',
                           (body['message_id'],path,began,ended,outcome,reason,canonical(receipt) if receipt else None))
                db.execute('UPDATE outbox SET attempts=attempts+1,ack_ms=? WHERE idx=?',(ended if outcome=='acked' else None,row['idx']))
                put_state(db,'controller',selector.snapshot() if policy=='adaptive' else {'policy':'fixed_a'})
                put_state(db,'active_path',path)
            if policy=='fixed_a' and outcome!='acked': time.sleep(.05)
        return 0
    finally:
        server.shutdown(); server.server_close(); receiver_thread.join(timeout=2)


def phase_metrics(attempts,phases,finished_ms,node_results=None):
    result=[]
    for i,phase in enumerate(phases):
        end=phases[i+1]['started_ms'] if i+1<len(phases) else finished_ms
        start=phase['applied_ms']; guarded=start+200
        row={'profile':phase['profile'],'applied_ms':start,'until_ms':end,'guard_ms':200,'directions':{}}
        for node in NODES:
            records=attempts[node]
            successes=[r for r in records if r['outcome']=='acked' and r['started_ms']>=guarded and r['ended_ms']<end]
            after=[r for r in records if r['outcome']=='acked' and r['started_ms']>=start and r['ended_ms']<end]
            row['directions'][node]={'new_acked':len(successes),'acked_by_path':{p:sum(r['path']==p for r in successes) for p in PATHS},
                'first_new_ack_ms':min((r['ended_ms']-start for r in after),default=None),
                'attempts':sum(start<=r['started_ms']<end for r in records)}
            receiver='right' if node=='left' else 'left'
            accepted=(node_results or {}).get(receiver,{}).get('accepted_events',[])
            new_effects=sum(guarded<=e['received_ms']<end for e in accepted)
            row['directions'][node].update(new_effects=new_effects,new_bytes=new_effects*CHUNK_BYTES)
        result.append(row)
    return result


def node_result(queue,node,count):
    other='right' if node=='left' else 'left'
    with connect(queue) as db:
        received=list(db.execute('SELECT * FROM inbox ORDER BY idx'))
        received_bytes=b''.join(bytes(r['data']) for r in received)
        Path(queue).with_name('received-'+node+'.bin').write_bytes(received_bytes)
        expected=b''.join(chunk(other,i) for i in range(count))
        attempts=[dict(r) for r in db.execute('SELECT message_id,path,started_ms,ended_ms,outcome,selection_reason FROM attempts ORDER BY id')]
        status=node_status(queue)
        return {'status':status,'received_chunks':len(received),'received_bytes':len(received_bytes),
                'expected_bytes':len(expected),'received_file_sha256':hashlib.sha256(received_bytes).hexdigest(),
                'expected_file_sha256':hashlib.sha256(expected).hexdigest(),
                'file_exact':received_bytes==expected,'effects_unique':len({r['message_id'] for r in received})==len(received),
                'accepted_events':[{'index':r['idx'],'received_ms':r['received_ms'],'sha256':r['sha256']} for r in received]},attempts


def stop(process):
    if process.poll() is None:
        process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
    return process.poll() is not None


def source_hashes():
    base=Path(__file__).parent
    return {name:hashlib.sha256((base/name).read_bytes()).hexdigest() for name in ('multipath_vm_lab.py','multipath_policy.py')}


def run(output,token_file,policy='adaptive',scenario='standard',count=COUNT,interval_ms=INTERVAL_MS):
    require(platform.system()=='Linux' and os.geteuid()==0,'linux_root_required')
    require(all(shutil.which(x) for x in ('ip','tc')),'iproute2_required')
    require(policy in ('adaptive','fixed_a') and scenario in SCENARIOS,'unknown_mode')
    require(type(count) is int and 196<=count<=300 and type(interval_ms) is int and 250<=interval_ms<=300,'workload_bounds')
    require(count*interval_ms>=49000 and count*interval_ms<=90000,'duration_bounds')
    token_from(token_file); token_file=Path(token_file).resolve()
    output=Path(output).absolute(); require(not output.exists() and not output.is_symlink(),'new_output_required')
    output.mkdir(parents=True,mode=0o700)
    run_id='multipath-'+uuid.uuid4().hex
    report={'schema':'sawm-multipath-report/v1','run_id':run_id,'policy':policy,'scenario':scenario,
            'scope':{'real_tcp_http':True,'synthetic_data':True,'nodes':2,'virtual_paths':4,'one_linux_host':True,
                     'physical_radio':False,'cloud_calls':False,'odvl_in_selector':False,'physical_independence':False},
            'workload':{'chunks_per_direction':count,'bytes_per_chunk':CHUNK_BYTES,'interval_ms':interval_ms},
            'source_sha256':source_hashes(),'phase_events':[],'checks':{},'started_at_ms':time.time_ns()//1_000_000}
    before=primary_signature(); links=Links(); processes={}; logs=[]; start=time.monotonic_ns(); old_handlers={}
    def interrupted(signum,frame): raise InterruptedError('laboratory_interrupted')
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM,signal.SIGINT): old_handlers[signum]=signal.signal(signum,interrupted)
    progress=None
    try:
        topology=links.create(); save(output/'topology-private.json',topology)
        for node in NODES:
            queue=output/(node+'.sqlite'); initialize(queue)
            log=(output/(node+'.log')).open('w'); logs.append(log)
            argv=ns(links.names[node],sys.executable,str(Path(__file__).resolve()),'--node',node,
                    '--namespace',links.names[node],'--queue',str(queue),'--start-file',str(output/'start.json'),
                    '--token-file',str(token_file),'--run-id',run_id,'--messages',str(count),'--interval-ms',str(interval_ms),
                    '--policy',policy)
            processes[node]=subprocess.Popen(argv,stdout=log,stderr=subprocess.STDOUT)
        ready_deadline=time.monotonic()+10
        while not all(Path(str(output/(n+'.sqlite'))+'.ready').exists() for n in NODES):
            require(time.monotonic()<ready_deadline,'node_start_timeout')
            require(all(p.poll() is None for p in processes.values()),'node_start_failed'); time.sleep(.05)
        start=time.monotonic_ns()
        report['phase_events'].append(links.apply('nominal',start))
        save(output/'start.json',{'start_ns':start})
        phase='nominal'; next_status=0
        while elapsed(start)<BUDGET_SECONDS*1000:
            now=elapsed(start)
            desired=next(p for t,p in reversed(SCENARIOS[scenario]) if now>=t)
            if desired!=phase:
                report['phase_events'].append(links.apply(desired,start)); phase=desired
                save(output/'phase-events.json',report['phase_events'])
            require(all(p.poll() is None for p in processes.values()),'node_exited_early')
            if now>=next_status:
                nodes={n:node_status(output/(n+'.sqlite')) for n in NODES}
                progress={'schema':'sawm-multipath-progress/v1','run_id':run_id,'updated_at_ms':time.time_ns()//1_000_000,
                          'elapsed_ms':elapsed(start),'phase':phase,'policy':policy,'scenario':scenario,
                          'completed':False,'success':None,'nodes':nodes,'phase_events':report['phase_events']}
                save(output/'progress.json',progress)
                if phase=='recovery' and all(v['acked']==count and v['received']==count for v in nodes.values()): break
                next_status=elapsed(start)+250
            time.sleep(.02)
        report['elapsed_ms']=elapsed(start)
        report['node_results']={}; attempts={}
        for node in NODES:
            report['node_results'][node],attempts[node]=node_result(output/(node+'.sqlite'),node,count)
        save(output/'attempts.json',attempts)
        report['phase_metrics']=phase_metrics(attempts,report['phase_events'],report['elapsed_ms'],report['node_results'])
        report['nodes']={n:r['status'] for n,r in report['node_results'].items()}
        for node in NODES:
            result=report['node_results'][node]
            report['checks'][node+'_all_acked']=result['status']['acked']==count
            report['checks'][node+'_file_exact']=result['file_exact']
            report['checks'][node+'_no_duplicate_effects']=result['effects_unique'] and result['status']['conflicts']==0
        report['checks']['all_scenario_phases']=len(report['phase_events'])==len(SCENARIOS[scenario])
        for i,metric in enumerate(report['phase_metrics']):
            if metric['profile']=='all_blocked':
                report['checks']['all_blocked_no_new_ack']=all(d['new_acked']==0 for d in metric['directions'].values())
                phase=report['phase_events'][i]
                report['checks']['all_blocked_no_new_effect']=all(not any(phase['applied_ms']+200<=e['received_ms']<metric['until_ms']
                    for e in r['accepted_events']) for r in report['node_results'].values())
            elif policy=='adaptive':
                report['checks'][f'phase_{i}_{metric["profile"]}_bidirectional']=all(d['new_acked']>0 and d['new_effects']>0 for d in metric['directions'].values())
                allowed={p for p,v in report['phase_events'][i]['paths'].items() if v['loss_pct']<100}
                report['checks'][f'phase_{i}_only_available_paths']=all(not any(v for p,v in d['acked_by_path'].items() if p not in allowed)
                    for d in metric['directions'].values())
    except Exception as exc:
        report['error']=type(exc).__name__+':'+str(exc)[:240]
    finally:
        for signum in old_handlers: signal.signal(signum,signal.SIG_IGN)
        cleanup={}
        for node,process in processes.items():
            try: cleanup[node+'_process_stopped']=stop(process)
            except Exception: cleanup[node+'_process_stopped']=False
        for log in logs: log.close()
        try: cleanup['network_removed']=all(links.close().values())
        except Exception: cleanup['network_removed']=False
        report['cleanup']=cleanup
        try: report['checks']['primary_network_preserved']=primary_signature()==before
        except Exception: report['checks']['primary_network_preserved']=False
        report['checks']['cleanup']=bool(cleanup) and all(cleanup.values())
        report['checks']['source_unchanged']=source_hashes()==report['source_sha256']
        report['success']='error' not in report and bool(report['checks']) and all(report['checks'].values())
        report['finished_at_ms']=time.time_ns()//1_000_000
        report['elapsed_ms']=report.get('elapsed_ms',elapsed(start))
        save(output/'setup-cleanup-private.json',links.log)
        save(output/'phase-events.json',report['phase_events'])
        save(output/'report.json',report)
        if progress:
            progress.update(completed=True,success=report['success'],updated_at_ms=time.time_ns()//1_000_000)
            progress['nodes']={n:node_status(output/(n+'.sqlite')) for n in NODES}
            save(output/'progress.json',progress)
        for signum,handler in old_handlers.items(): signal.signal(signum,handler)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Create only this isolated laboratory network, then clean it up.')
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--policy',choices=('adaptive','fixed_a'),default='adaptive')
    parser.add_argument('--scenario',choices=tuple(SCENARIOS),default='standard')
    parser.add_argument('--messages',type=int,default=COUNT)
    parser.add_argument('--interval-ms',type=int,default=INTERVAL_MS)
    parser.add_argument('--node',choices=NODES,help=argparse.SUPPRESS)
    parser.add_argument('--namespace',help=argparse.SUPPRESS)
    parser.add_argument('--queue',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--start-file',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--run-id',help=argparse.SUPPRESS)
    args=parser.parse_args()
    try:
        if args.node:
            require(all((args.namespace,args.queue,args.start_file,args.run_id)),'node_arguments')
            return node_run(args.node,args.namespace,args.queue,args.start_file,args.token_file,args.run_id,
                            args.messages,args.interval_ms,args.policy,BUDGET_SECONDS)
        require(args.run and args.output_dir is not None,'explicit_run_and_output_required')
        result=run(args.output_dir,args.token_file,args.policy,args.scenario,args.messages,args.interval_ms)
        print(canonical({'run_id':result['run_id'],'success':result['success'],'checks':result['checks'],'error':result.get('error')}))
        return 0 if result['success'] else 2
    except Exception as exc:
        print(canonical({'error':type(exc).__name__+':'+str(exc)[:240]}),file=sys.stderr); return 2


if __name__=='__main__':
    sys.exit(main())
