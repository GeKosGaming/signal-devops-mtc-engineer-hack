"""Real local OpenSSL + random generation; mock only the Kubernetes boundary."""
import base64, contextlib, io, pathlib, shutil, sys, tempfile, unittest
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import create_secrets

@unittest.skipUnless(shutil.which('openssl'),'OpenSSL required for credential idempotence test')
class CredentialGeneration(unittest.TestCase):
    def test_new_credentials_then_idempotent_second_run(self):
        store={}; writes=[]
        def get(name,ns):return store.get((name,ns))
        def apply(obj):
            writes.append(obj);store[(obj['metadata']['name'],obj['metadata']['namespace'])]=obj
        with tempfile.TemporaryDirectory(prefix='signal-secret-test-') as directory:
            with patch.object(create_secrets,'ROOT',pathlib.Path(directory)),patch.object(create_secrets,'get',side_effect=get),patch.object(create_secrets,'apply',side_effect=apply):
                output=io.StringIO()
                with contextlib.redirect_stdout(output):create_secrets.main()
                self.assertEqual(len(writes),2)
                password=base64.b64decode(store[('grafana-admin','signal-observe')]['data']['password']).decode()
                self.assertGreaterEqual(len(password),40);self.assertNotIn(password,output.getvalue())
                cert=(pathlib.Path(directory)/'.state/tls/server.crt').read_bytes()
                key=(pathlib.Path(directory)/'.state/tls/server.key').read_bytes()
                with contextlib.redirect_stdout(io.StringIO()):create_secrets.main()
                self.assertEqual(len(writes),2)
                self.assertEqual(cert,(pathlib.Path(directory)/'.state/tls/server.crt').read_bytes())
                self.assertEqual(key,(pathlib.Path(directory)/'.state/tls/server.key').read_bytes())
