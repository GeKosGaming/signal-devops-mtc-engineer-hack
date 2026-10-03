"""Real local nginx integration; not a replacement for the pinned image or Kubernetes E2E."""
from __future__ import annotations
import json, pathlib, shutil, socket, subprocess, sys, tempfile, time, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import render, runtime

@unittest.skipUnless(shutil.which('nginx'), 'Local nginx not installed; runtime image checks are separate')
class LocalNginx(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='signal-nginx-');cls.path=pathlib.Path(cls.tmp.name);cls.path.chmod(0o755)
        cls.servers=[];cls.ports=[]
        cls.addClassCleanup(cls.cleanup)
        for index,broken in enumerate((False,True)):
            root=cls.path/str(index);root.mkdir(mode=0o755)
            def port():
                with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
            p,q=port(),port();cls.ports.append(p)
            conf=render.nginx_config('canary' if broken else 'stable',broken)
            conf=conf.replace('worker_processes auto','worker_processes 1').replace('/tmp/',str(root)+'/')
            conf=conf.replace('listen 8080;',f'listen 127.0.0.1:{p};').replace('127.0.0.1:8081',f'127.0.0.1:{q}')
            conf=conf.replace('/dev/stdout',str(root/'access.log')).replace('/dev/stderr',str(root/'error.log'))
            c=root/'nginx.conf';c.write_text(conf)
            subprocess.run(['nginx','-t','-c',str(c),'-p',str(root)],check=True,capture_output=True)
            proc=subprocess.Popen(['nginx','-c',str(c),'-p',str(root),'-g','daemon off;'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            cls.servers.append(proc)
            runtime.retry(lambda:runtime.http(f'http://127.0.0.1:{p}/healthz'),10,.1)
    @classmethod
    def cleanup(cls):
        for proc in cls.servers:
            proc.terminate()
            try:proc.wait(timeout=4)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
        cls.tmp.cleanup()
    def req(self,path='/',headers=None,broken=False):return runtime.http(f'http://127.0.0.1:{self.ports[int(broken)]}'+path,headers=headers)
    def test_exact_body_and_release(self):
        r=self.req();self.assertEqual(r['body'],'Hello World!\n');self.assertEqual(r['status'],200);self.assertEqual(r['headers']['x-release'],'stable')
    def test_health_endpoint(self):self.assertEqual(self.req('/healthz')['body'],'ok\n')
    def test_proof_echo(self):self.assertEqual(self.req(headers={'X-Proof-ID':'proof-123'})['headers']['x-proof-id'],'proof-123')
    def test_invalid_proof_sanitized(self):
        value=self.req(headers={'X-Proof-ID':'bad?value'})['headers']['x-proof-id'];self.assertNotEqual(value,'bad?value');self.assertRegex(value,r'^[a-f0-9]{32}$')
    def test_json_access_logged(self):
        self.req('/unique-access?private=not-logged',{'X-Proof-ID':'access-proof'})
        rows=[json.loads(x) for x in (self.path/'0/access.log').read_text().splitlines()]
        row=next(x for x in rows if x['proof_id']=='access-proof')
        self.assertEqual(row['status'],200);self.assertEqual(row['uri'],'/unique-access');self.assertNotIn('not-logged',json.dumps(row))
    def test_real_missing_file_error(self):
        self.assertEqual(self.req('/missing/proof-error-local')['status'],404)
        self.assertIn('proof-error-local',(self.path/'0/error.log').read_text())
    def test_injected_fault_business_failure(self):self.assertEqual(self.req(broken=True)['status'],503)
    def test_injected_fault_not_probe_failure(self):self.assertEqual(self.req('/healthz',broken=True)['status'],200)

if __name__=='__main__':unittest.main()
