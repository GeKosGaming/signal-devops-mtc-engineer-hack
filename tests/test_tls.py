"""Actual TLS handshake test: validation, custom connect address and preserved SNI."""
import http.server, pathlib, shutil, ssl, subprocess, sys, tempfile, threading, unittest
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import runtime

@unittest.skipUnless(shutil.which('openssl'),'OpenSSL is required for local TLS handshake tests')
class TLS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix='signal-tls-test-');p=pathlib.Path(cls.tmp.name);cls.cert=p/'cert.pem';cls.sni=[]
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=signal.local',
                        '-keyout',str(p/'key.pem'),'-out',str(cls.cert),'-addext','subjectAltName=DNS:signal.local'],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'TLS validated\n')
            def log_message(self,*args):pass
        cls.server=http.server.HTTPServer(('127.0.0.1',0),Handler);ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cls.cert,p/'key.pem');ctx.set_servername_callback(lambda sock,name,context: cls.sni.append(name))
        cls.server.socket=ctx.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join(3);cls.tmp.cleanup()
    def test_correct_sni_and_ca(self):
        port=self.server.server_port
        r=runtime.http(f'https://signal.local:{port}/',ca=self.cert,connect_ip='127.0.0.1')
        self.assertEqual(r['status'],200);self.assertEqual(r['body'],'TLS validated\n');self.assertIn('signal.local',self.sni)
    def test_untrusted_ca_rejected(self):
        with self.assertRaises(OSError):runtime.http(f'https://signal.local:{self.server.server_port}/',connect_ip='127.0.0.1')
    def test_wrong_hostname_rejected(self):
        with self.assertRaises(OSError):runtime.http(f'https://not-signal.invalid:{self.server.server_port}/',ca=self.cert,connect_ip='127.0.0.1')
