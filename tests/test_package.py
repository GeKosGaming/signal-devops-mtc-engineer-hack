"""Packaging gates using real temporary files and mocked Git/network boundaries."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import package

HEAD='a'*40
URL='https://github.com/example/signal.git'


class Packaging(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='signal-package-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=pathlib.Path(self.temp.name)
        self.patch_root=patch.object(package,'ROOT',self.root)
        self.patch_root.start();self.addCleanup(self.patch_root.stop)
        for directory in ['config','vendor','docs','evidence/acceptance','evidence/operations']:
            (self.root/directory).mkdir(parents=True)
        self.original={'nginx':'docker.io/library/nginx:1.28.0-alpine'}
        self.lock={'nginx':self.original['nginx']+'@sha256:'+'b'*64}
        self.write_json('config/images.json',self.original)
        self.write_json('config/images.lock.json',self.lock)
        (self.root/'versions.env').write_text('CILIUM_VERSION=1.19.2\nENVOY_GATEWAY_VERSION=v1.7.0\n')
        self.charts=['cilium-1.19.2.tgz','gateway-helm-v1.7.0.tgz']
        for name in self.charts:(self.root/'vendor'/name).write_bytes(('chart '+name).encode())
        self.checksums()
        self.proof={'passed':True,'ubuntu_24_04_kubeadm_confirmed':True,
                    'environment':{'git_commit':HEAD,'git_worktree_clean':True}}
        self.write_json('evidence/acceptance/report.json',self.proof)
        self.safety={'passed':True,'environment':{'git_commit':HEAD,'git_worktree_clean':True},
                     'checks':[{'name':name,'passed':True} for name in [
                         'safe baseline before operation probes','concurrent mutations rejected before cluster changes',
                         'SIGTERM after actual faulty canary injection restores baseline',
                         'SIGINT during healthy promotion restores baseline','stable baseline after operation probes']]}
        self.write_json('evidence/operations/report.json',self.safety)
        self.passport=self.root/'docs/Паспорт.pdf';self.passport.write_bytes(b'%PDF-1.7\nfixture\n')
        self.committed={path.relative_to(self.root).as_posix():path.read_bytes()
                        for path in self.root.rglob('*') if path.is_file()}

    def write_json(self,path,data):
        (self.root/path).write_text(json.dumps(data),encoding='utf-8')

    def checksums(self):
        entries=[hashlib.sha256((self.root/'vendor'/name).read_bytes()).hexdigest()+'  '+name
                 for name in self.charts]
        (self.root/'vendor/SHA256SUMS').write_text('\n'.join(entries)+'\n')

    def git_blob(self,command,**kwargs):
        self.assertEqual(command[:3],['git','cat-file','blob'])
        self.assertEqual(kwargs['cwd'],self.root)
        ref,name=command[3].split(':',1);self.assertEqual(ref,HEAD)
        if name not in self.committed:raise subprocess.CalledProcessError(128,command,stderr=b'missing')
        return SimpleNamespace(stdout=self.committed[name])

    def git_status(self,*args):
        return {('rev-parse','HEAD'):HEAD,('branch','--show-current'):'main',
                ('status','--porcelain','--untracked-files=normal'):''}[args]

    def test_complete_bundle_requires_every_dependency_in_head(self):
        with patch.object(package.subprocess,'run',side_effect=self.git_blob) as run:
            package.dependencies(HEAD)
        names={call.args[0][3].split(':',1)[1] for call in run.call_args_list}
        self.assertEqual(names,{'config/images.json','config/images.lock.json','versions.env',
                               'vendor/SHA256SUMS',*(f'vendor/{name}' for name in self.charts)})

    def test_missing_chart_rejected_even_with_checksum_file(self):
        (self.root/'vendor'/self.charts[0]).unlink()
        with self.assertRaisesRegex(ValueError,'chart is missing'):package.dependencies(HEAD)

    def test_chart_corruption_rejected(self):
        (self.root/'vendor'/self.charts[0]).write_bytes(b'changed chart')
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):package.dependencies(HEAD)

    def test_old_chart_versions_rejected(self):
        (self.root/'versions.env').write_text('CILIUM_VERSION=1.19.3\nENVOY_GATEWAY_VERSION=v1.7.0\n')
        with self.assertRaisesRegex(ValueError,'exactly the two configured'):package.dependencies(HEAD)

    def test_duplicate_checksum_entries_rejected(self):
        path=self.root/'vendor/SHA256SUMS'
        path.write_text(path.read_text()+path.read_text().splitlines()[0]+'\n')
        with self.assertRaisesRegex(ValueError,'duplicate'):package.dependencies(HEAD)

    def test_lock_for_different_image_tag_rejected(self):
        self.lock['nginx']=self.lock['nginx'].replace('1.28.0','1.27.0')
        self.write_json('config/images.lock.json',self.lock)
        with self.assertRaisesRegex(ValueError,'Stale or invalid image lock'):package.dependencies(HEAD)

    def test_missing_lock_image_rejected(self):
        self.write_json('config/images.lock.json',{})
        with self.assertRaisesRegex(ValueError,'lock keys'):package.dependencies(HEAD)

    def test_uncommitted_chart_rejected(self):
        del self.committed['vendor/'+self.charts[0]]
        with patch.object(package.subprocess,'run',side_effect=self.git_blob):
            with self.assertRaisesRegex(ValueError,'missing from HEAD'):package.dependencies(HEAD)

    def test_uncommitted_lock_rejected(self):
        del self.committed['config/images.lock.json']
        with patch.object(package.subprocess,'run',side_effect=self.git_blob):
            with self.assertRaisesRegex(ValueError,'missing from HEAD'):package.dependencies(HEAD)

    def test_dependency_content_different_from_commit_rejected(self):
        self.committed['vendor/'+self.charts[0]]=b'other committed chart'
        with patch.object(package.subprocess,'run',side_effect=self.git_blob):
            with self.assertRaisesRegex(ValueError,'differs from HEAD'):package.dependencies(HEAD)

    def acceptance(self):
        self.write_json('evidence/acceptance/report.json',self.proof)
        with patch.object(package,'git_text',side_effect=self.git_status),patch.object(package,'dependencies'):
            return package.accepted_commit()

    def test_clean_current_acceptance_passes(self):self.assertEqual(self.acceptance(),HEAD)

    def test_missing_operation_safety_rejected(self):
        (self.root/'evidence/operations/report.json').unlink()
        with self.assertRaisesRegex(ValueError,'operation-check'):self.acceptance()

    def test_failed_stale_dirty_or_incomplete_operation_safety_rejected(self):
        import copy
        for changed in ('failed','stale','dirty','incomplete','failed_check'):
            with self.subTest(changed=changed):
                proof=copy.deepcopy(self.safety)
                if changed=='failed':proof['passed']=False
                elif changed=='stale':proof['environment']['git_commit']='c'*40
                elif changed=='dirty':proof['environment']['git_worktree_clean']=False
                elif changed=='incomplete':proof['checks'].pop()
                else:proof['checks'][0]['passed']=False
                self.write_json('evidence/operations/report.json',proof)
                with self.assertRaisesRegex(ValueError,'Operation safety acceptance'):self.acceptance()

    def test_old_acceptance_commit_rejected(self):
        self.proof['environment']['git_commit']='c'*40
        with self.assertRaisesRegex(ValueError,'submitted code commit'):self.acceptance()

    def test_acceptance_from_dirty_worktree_rejected(self):
        self.proof['environment']['git_worktree_clean']=False
        with self.assertRaisesRegex(ValueError,'clean Git working tree'):self.acceptance()

    def test_old_report_without_clean_worktree_proof_rejected(self):
        del self.proof['environment']['git_worktree_clean']
        with self.assertRaisesRegex(ValueError,'clean Git working tree'):self.acceptance()

    def test_current_dirty_worktree_rejected(self):
        def dirty(*args):return ' M scripts/render.py' if args[0]=='status' else self.git_status(*args)
        with patch.object(package,'git_text',side_effect=dirty):
            with self.assertRaisesRegex(ValueError,'Working tree is not clean'):package.accepted_commit()

    def test_string_pass_is_not_accepted_as_boolean(self):
        self.proof['passed']='true'
        with self.assertRaisesRegex(ValueError,'has not passed'):self.acceptance()

    def external(self,command,**kwargs):
        if command[0]=='pdfinfo':return SimpleNamespace(stdout='Pages:          4\n')
        self.assertEqual(command[:6],['git','-c','credential.helper=','-c','http.extraHeader=','ls-remote'])
        self.assertNotEqual(pathlib.Path(kwargs['cwd']),self.root)
        env=kwargs['env']
        self.assertEqual(env['GIT_TERMINAL_PROMPT'],'0')
        self.assertEqual(env['GIT_CONFIG_GLOBAL'],'/dev/null')
        self.assertNotIn('GIT_CONFIG_COUNT',env)
        self.assertNotIn('GIT_CONFIG_KEY_0',env)
        self.assertNotIn('GIT_CONFIG_VALUE_0',env)
        if '--symref' in command:return SimpleNamespace(stdout='ref: refs/heads/main\tHEAD\n'+HEAD+'\tHEAD\n')
        return SimpleNamespace(stdout=HEAD+'\trefs/heads/main\n')

    def build(self,external=None,git_status=None):
        with patch.object(package,'accepted_commit',return_value=HEAD),\
             patch.object(package,'git_text',side_effect=git_status or self.git_status),\
             patch.object(package.subprocess,'run',side_effect=external or self.external):
            return package.build(URL,'Коннов',self.passport)

    def test_archive_contains_only_url_and_passport_and_uses_anonymous_remote_check(self):
        with patch.dict(os.environ,{'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'http.extraHeader',
                                    'GIT_CONFIG_VALUE_0':'Authorization: PRIVATE'}):
            result=self.build()
        self.assertEqual(result.name,'Коннов.zip')
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(archive.namelist(),['Ссылка.txt','Паспорт.pdf'])
            self.assertEqual(archive.read('Ссылка.txt').decode(),URL+'\n')
            self.assertEqual(archive.read('Паспорт.pdf'),self.passport.read_bytes())

    def test_credential_url_rejected_without_external_action(self):
        with patch.object(package.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'without credentials'):
                package.build('https://user:secret@github.com/example/signal.git','Коннов',self.passport)
        run.assert_not_called()

    def test_remote_commit_mismatch_blocks_archive(self):
        def wrong(command,**kwargs):
            if command[0]=='pdfinfo':return self.external(command,**kwargs)
            return SimpleNamespace(stdout='c'*40+'\trefs/heads/main\n')
        with self.assertRaisesRegex(ValueError,'Public main does not match'):self.build(wrong)
        self.assertFalse((self.root/'submission/Коннов.zip').exists())

    def test_passport_over_four_pages_blocks_remote_check(self):
        def five_pages(command,**kwargs):
            self.assertEqual(command[0],'pdfinfo');return SimpleNamespace(stdout='Pages: 5\n')
        with self.assertRaisesRegex(ValueError,'1–4 pages'):self.build(five_pages)

    def test_change_during_remote_check_blocks_archive(self):
        def changed(*args):return 'c'*40 if args[0]=='rev-parse' else self.git_status(*args)
        with self.assertRaisesRegex(ValueError,'changed during packaging'):self.build(git_status=changed)
        self.assertFalse((self.root/'submission/Коннов.zip').exists())


if __name__=='__main__':unittest.main()
