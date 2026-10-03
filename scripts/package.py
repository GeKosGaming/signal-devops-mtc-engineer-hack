#!/usr/bin/env python3
"""Create a two-file submission after clean-commit acceptance and public main checks."""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, tempfile, urllib.parse, zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def git_text(*args: str) -> str:
    return subprocess.run(['git',*args],cwd=ROOT,check=True,capture_output=True,text=True).stdout.strip()

def require_committed(path: Path, head: str) -> None:
    """Require the actual file contents to be present in the accepted Git commit."""
    relative=path.relative_to(ROOT).as_posix()
    try:
        committed=subprocess.run(['git','cat-file','blob',f'{head}:{relative}'],cwd=ROOT,
                                 check=True,capture_output=True).stdout
    except subprocess.CalledProcessError as exc:
        raise ValueError(f'Required dependency is missing from HEAD: {relative}') from exc
    if path.is_symlink() or committed!=path.read_bytes():
        raise ValueError(f'Required dependency differs from HEAD: {relative}')

def dependencies(head: str) -> None:
    originals_path=ROOT/'config/images.json';locked_path=ROOT/'config/images.lock.json'
    if not locked_path.is_file():
        raise ValueError('Run make lock before final deployment/acceptance and commit the lock')
    originals=json.loads(originals_path.read_text(encoding='utf-8'))
    frozen=json.loads(locked_path.read_text(encoding='utf-8'))
    if not isinstance(frozen,dict) or set(frozen)!=set(originals):
        raise ValueError('Image lock keys do not match config/images.json')
    for key,original in originals.items():
        value=frozen[key]
        if not isinstance(value,str) or not re.fullmatch(re.escape(original)+r'@sha256:[a-f0-9]{64}',value):
            raise ValueError(f'Stale or invalid image lock: {key}')
    versions_path=ROOT/'versions.env';versions={}
    for line in versions_path.read_text(encoding='utf-8').splitlines():
        match=re.fullmatch(r'(CILIUM_VERSION|ENVOY_GATEWAY_VERSION)=(v?\d+\.\d+\.\d+)',line.strip())
        if match:versions[match[1]]=match[2]
    if set(versions)!={'CILIUM_VERSION','ENVOY_GATEWAY_VERSION'}:
        raise ValueError('Missing or invalid chart versions in versions.env')
    expected={f'cilium-{versions["CILIUM_VERSION"]}.tgz',f'gateway-helm-{versions["ENVOY_GATEWAY_VERSION"]}.tgz'}
    checksum_path=ROOT/'vendor/SHA256SUMS'
    if not checksum_path.is_file():
        raise ValueError('Run make vendor and commit the two charts and checksums')
    checksums={}
    for line in checksum_path.read_text(encoding='utf-8').splitlines():
        match=re.fullmatch(r'([a-fA-F0-9]{64}) [ *](.+)',line)
        if not match or match[2] in checksums:
            raise ValueError('Malformed or duplicate vendor checksum entry')
        checksums[match[2]]=match[1].lower()
    if set(checksums)!=expected:
        raise ValueError('Vendor checksums must name exactly the two configured chart versions')
    charts=[]
    for name in sorted(expected):
        path=ROOT/'vendor'/name
        if not path.is_file() or path.is_symlink():raise ValueError(f'Required vendored chart is missing: {name}')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=checksums[name]:
            raise ValueError(f'Vendored chart checksum mismatch: {name}')
        charts.append(path)
    for path in [originals_path,locked_path,versions_path,checksum_path,*charts]:require_committed(path,head)

def accepted_commit() -> str:
    head=git_text('rev-parse','HEAD')
    if git_text('branch','--show-current')!='main':raise ValueError('Submission must be on main')
    if git_text('status','--porcelain','--untracked-files=normal'):
        raise ValueError('Working tree is not clean. Keep runtime evidence ignored; see docs/SUBMISSION.md')
    status=ROOT/'evidence/acceptance/report.json'
    if not status.is_file():raise ValueError('Run make acceptance on Ubuntu 24.04 with kubeadm before packaging')
    proof=json.loads(status.read_text(encoding='utf-8'))
    if proof.get('passed') is not True or proof.get('ubuntu_24_04_kubeadm_confirmed') is not True:
        raise ValueError('Ubuntu kubeadm acceptance has not passed')
    environment=proof.get('environment',{})
    if environment.get('git_commit')!=head:
        raise ValueError('Acceptance must refer to the submitted code commit; see docs/SUBMISSION.md')
    if environment.get('git_worktree_clean') is not True:
        raise ValueError('Acceptance must have run with a clean Git working tree')
    dependencies(head)
    return head

def build(repo_url: str, surname: str, passport: Path) -> Path:
    repo_url=repo_url.strip()
    url=urllib.parse.urlsplit(repo_url)
    if (url.scheme!='https' or not url.netloc or url.username or url.password or url.query or url.fragment
            or any(char.isspace() for char in repo_url)):
        raise ValueError('REPO_URL must be a public HTTPS clone URL without credentials, query or fragment')
    if not re.fullmatch(r'[\w-]{1,80}',surname,flags=re.UNICODE):raise ValueError('Supply the surname used at registration; no paths/spaces')
    head=accepted_commit()
    passport=passport.resolve()
    if not passport.is_file() or passport.suffix.lower()!='.pdf' or passport.stat().st_size>15_000_000:
        raise ValueError('Passport must be an existing PDF <=15 MB')
    with passport.open('rb') as f:
        if f.read(5)!=b'%PDF-':raise ValueError('Not a PDF')
    # poppler-utils is only needed for submission packaging, not for deployment.
    info=subprocess.run(['pdfinfo',str(passport)],check=True,capture_output=True,text=True).stdout
    count=re.search(r'^Pages:\s+(\d+)',info,re.M)
    if not count or not 1<=int(count[1])<=4:raise ValueError('Passport must have 1–4 pages')
    # Read-only anonymous verification; never creates, pushes, publishes or changes the user's repository.
    env={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    env.update({'GIT_TERMINAL_PROMPT':'0','GIT_ASKPASS':'/bin/false','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null'})
    # Use a fresh cwd as well, so local repository http.extraHeader/credential configuration cannot leak into this check.
    with tempfile.TemporaryDirectory(prefix='signal-public-check-') as directory:
        command=['git','-c','credential.helper=','-c','http.extraHeader=','ls-remote']
        remote=subprocess.run(command+['--exit-code',repo_url,'refs/heads/main'],cwd=directory,
                              env=env,check=True,capture_output=True,text=True,timeout=45).stdout.split()[0]
        if remote!=head:raise ValueError('Public main does not match the local commit')
        default=subprocess.run(command+['--symref',repo_url,'HEAD'],cwd=directory,env=env,check=True,
                               capture_output=True,text=True,timeout=45).stdout
        if 'ref: refs/heads/main\tHEAD' not in default:raise ValueError('Public default branch must be main')
    # Recheck after the external read: concurrent local changes must not enter the submission.
    if git_text('rev-parse','HEAD')!=head or git_text('status','--porcelain','--untracked-files=normal'):
        raise ValueError('Git commit or working tree changed during packaging')
    dest=ROOT/'submission'/f'{surname}.zip';dest.parent.mkdir(exist_ok=True)
    # A clone URL resolves the default branch; public main has just been verified explicitly.
    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('Ссылка.txt',repo_url.strip()+'\n');z.write(passport,'Паспорт.pdf')
    if dest.stat().st_size>18_000_000:dest.unlink();raise ValueError('Archive exceeds 18 MB')
    return dest

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repo-url',required=True);p.add_argument('--surname',required=True)
    p.add_argument('--passport',type=Path,default=ROOT/'docs/Паспорт.pdf');a=p.parse_args()
    print(build(a.repo_url,a.surname,a.passport))
if __name__=='__main__':main()
